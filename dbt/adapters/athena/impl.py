import re
from itertools import chain
from typing import Dict, Iterator, List, Optional, Set
from uuid import uuid4

import agate
import dbt.exceptions
from botocore.exceptions import ClientError
from dbt.adapters.athena import AthenaConnectionManager
from dbt.adapters.athena.relation import AthenaRelation, AthenaSchemaSearchMap
from dbt.adapters.base import available
from dbt.adapters.base.column import Column
from dbt.adapters.base.impl import GET_CATALOG_MACRO_NAME
from dbt.adapters.base.relation import InformationSchema
from dbt.adapters.sql import SQLAdapter
from dbt.clients.agate_helper import table_from_rows
from dbt.contracts.graph.compiled import CompileResultNode
from dbt.contracts.graph.manifest import Manifest
from dbt.contracts.relation import RelationType
from dbt.events import AdapterLogger

logger = AdapterLogger("Athena")


class AthenaAdapter(SQLAdapter):
    ConnectionManager = AthenaConnectionManager
    Relation = AthenaRelation
    Column = Column

    @classmethod
    def date_function(cls) -> str:
        return "now()"

    @classmethod
    def convert_text_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "string"

    @classmethod
    def convert_number_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        decimals = agate_table.aggregate(agate.MaxPrecision(col_idx))
        return "double" if decimals else "integer"

    @classmethod
    def convert_datetime_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "timestamp"

    def get_columns_in_relation(self, relation: Relation) -> List[Column]:
        cached_relation = self.get_relation(relation.database, relation.schema, relation.identifier)
        columns = []
        if cached_relation and cached_relation.column_information:
            for col_dict in cached_relation.column_information:
                column = Column.create(col_dict["Name"], col_dict["Type"])
                columns.append(column)
            return columns
        else:
            return super().get_columns_in_relation(relation)

    def list_relations_without_caching(self, schema_relation: AthenaRelation) -> List[AthenaRelation]:
        relations = []
        # Default quote policy of SQLAdapter
        quote_policy = {"database": True, "schema": True, "identifier": True}
        try:
            for table in self._retrieve_glue_tables(schema_relation.database, schema_relation.schema):
                rel_type = self._get_rel_type_from_glue_response(table)
                relation = self.Relation.create(
                    database=schema_relation.database,
                    identifier=table["Name"],
                    schema=schema_relation.schema,
                    quote_policy=quote_policy,
                    type=rel_type,
                    # StorageDescriptor.Columns doesn't include columns used as partition key
                    column_information=table["StorageDescriptor"]["Columns"] + table["PartitionKeys"],
                )
                relations.append(relation)
            return relations
        except ClientError as e:
            logger.debug(
                "Boto3 Error while retrieving relations. Fallback into SQL execution: code={}, message={}",
                e.response["Error"]["Code"],
                e.response["Error"].get("Message"),
            )
            # Fallback into SQL query
            return super().list_relations_without_caching(schema_relation)

    @available
    def s3_table_prefix(self) -> str:
        """
        Returns the root location for storing tables in S3.

        This is `s3_data_dir`, if set, and `s3_staging_dir/tables/` if not.

        We generate a value here even if `s3_data_dir` is not set,
        since creating a seed table requires a non-default location.
        """
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        if creds.s3_data_dir is not None:
            return creds.s3_data_dir
        else:
            return f"{creds.s3_staging_dir}tables/"

    @available
    def s3_uuid_table_location(self) -> str:
        """
        Returns a random location for storing a table, using a UUID as
        the final directory part
        """
        return f"{self.s3_table_prefix()}{str(uuid4())}/"


    @available
    def s3_schema_table_location(self, schema_name: str, table_name: str) -> str:
        """
        Returns a fixed location for storing a table determined by the
        (athena) schema and table name
        """
        return f"{self.s3_table_prefix()}{schema_name}/{table_name}/"

    @available
    def s3_table_location(self, schema_name: str, table_name: str) -> str:
        """
        Returns either a UUID or database/table prefix for storing a table,
        depending on the value of s3_table
        """
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        if creds.s3_data_naming == "schema_table":
            return self.s3_schema_table_location(schema_name, table_name)
        elif creds.s3_data_naming == "uuid":
            return self.s3_uuid_table_location()
        else:
            raise ValueError(f"Unknown value for s3_data_naming: {creds.s3_data_naming}")

    @available
    def has_s3_data_dir(self) -> bool:
        """
        Returns true if the user has specified `s3_data_dir`, and
        we should set `external_location
        """
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        return creds.s3_data_dir is not None


    @available
    def clean_up_partitions(self, database_name: str, table_name: str, where_condition: str):
        # Look up Glue partitions & clean up
        conn = self.connections.get_thread_connection()
        client = conn.handle
        glue_client = client.session.client("glue")
        s3_resource = client.session.resource("s3")
        partitions = glue_client.get_partitions(
            # CatalogId='123456789012', # Need to make this configurable if it is different from default AWS Account ID
            DatabaseName=database_name,
            TableName=table_name,
            Expression=where_condition,
        )
        p = re.compile("s3://([^/]*)/(.*)")
        for partition in partitions["Partitions"]:
            logger.debug(
                "Deleting objects for partition '{}' at '{}'",
                partition["Values"],
                partition["StorageDescriptor"]["Location"],
            )
            m = p.match(partition["StorageDescriptor"]["Location"])
            if m is not None:
                bucket_name = m.group(1)
                prefix = m.group(2)
                s3_bucket = s3_resource.Bucket(bucket_name)
                s3_bucket.objects.filter(Prefix=prefix).delete()

    @available
    def clean_up_table(self, database_name: str, table_name: str):
        # Look up Glue partitions & clean up
        conn = self.connections.get_thread_connection()
        client = conn.handle
        glue_client = client.session.client("glue")

        try:
            table = glue_client.get_table(DatabaseName=database_name, Name=table_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "EntityNotFoundException":
                logger.debug("Table '{}' does not exists - Ignoring", table_name)
                return

        if table is not None:
            logger.debug("Deleting table data from'{}'", table["Table"]["StorageDescriptor"]["Location"])
            p = re.compile("s3://([^/]*)/(.*)")
            m = p.match(table["Table"]["StorageDescriptor"]["Location"])
            if m is not None:
                bucket_name = m.group(1)
                prefix = m.group(2)

                s3_resource = client.session.resource("s3")
                s3_bucket = s3_resource.Bucket(bucket_name)
                s3_bucket.objects.filter(Prefix=prefix).delete()

    def _get_one_catalog(
        self,
        information_schema: InformationSchema,
        schemas: Set[str],
        manifest: Manifest,
    ) -> agate.Table:
        """hook macro get_catalog, and at first retrieving info via Glue API Directory"""
        # At first, we need to retrieve all schema name, and filter out from used schema lists
        target_database = information_schema.database
        used_schemas = frozenset(s.lower() for _, s in manifest.get_used_schemas())
        schema_list = self.list_schemas(target_database)
        target_schemas = [x for x in schema_list if x.lower() in used_schemas]

        try:
            rows = []
            for schema in target_schemas:
                for table in self._retrieve_glue_tables(target_database, schema):
                    rel_type = self._get_rel_type_from_glue_response(table)
                    # Important prefix: "table_", "column_", "stats:"
                    # Table key: database, schema, name
                    # Column key: type, index, name, comment
                    # Stats key: label, value, description, include
                    # Stats has a secondary prefix, user defined one.

                    # Table wide info
                    table_row = {
                        "table_database": target_database,
                        "table_schema": schema,
                        "table_name": table["Name"],
                        "table_type": rel_type,
                    }
                    # Additional info
                    descriptor = table["StorageDescriptor"]
                    table_row.update(
                        self._create_stats_dict("description", table.get("Description", ""), "Table description")
                    )
                    table_row.update(self._create_stats_dict("owner", table.get("Owner", ""), "Table owner"))
                    table_row.update(
                        self._create_stats_dict("created_at", str(table.get("CreateTime", "")), "Table creation time")
                    )
                    table_row.update(
                        self._create_stats_dict("updated_at", str(table.get("UpdateTime", "")), "Table update time")
                    )
                    table_row.update(self._create_stats_dict("created_by", table["CreatedBy"], "Who create it"))
                    table_row.update(self._create_stats_dict("partitions", table["PartitionKeys"], "Partition keys"))
                    table_row.update(self._create_stats_dict("location", descriptor.get("Location", ""), "Table path"))
                    table_row.update(
                        self._create_stats_dict("compressed", descriptor["Compressed"], "Table has compressed or not")
                    )
                    # each column info
                    for idx, col in enumerate(descriptor["Columns"] + table["PartitionKeys"]):
                        row = table_row.copy()
                        row.update(
                            {
                                "column_name": str(col["Name"]),
                                "column_type": str(col["Type"]),
                                "column_index": idx,
                                "column_comment": str(col.get("Comment", "")),
                            }
                        )
                        rows.append(row)

            if not rows:
                return table_from_rows([])  # Return empty table
            # rows is List[Dict], so iterate over each row as List[columns], List[column_names]
            column_names = list(rows[0].keys())  # dict key order is preserved in language level
            table = table_from_rows(
                [list(x.values()) for x in rows],
                column_names,
                text_only_columns=["table_database", "table_schema", "table_name"],
            )
            return self._catalog_filter_table(table, manifest)
        except ClientError as e:
            logger.debug(
                "Boto3 Error while retrieving catalog. Fallback into SQL execution: code={}, message={}",
                e.response["Error"]["Code"],
                e.response["Error"].get("Message"),
            )
            kwargs = {"information_schema": information_schema, "schemas": schemas}
            table = self.execute_macro(
                GET_CATALOG_MACRO_NAME,
                kwargs=kwargs,
                # pass in the full manifest so we get any local project
                # overrides
                manifest=manifest,
            )
            results = self._catalog_filter_table(table, manifest)
            return results

    def _retrieve_glue_tables(self, catalog_id: str, name: str):
        """Retrive Table informations through Glue API"""
        if not catalog_id:
            raise dbt.exceptions.RuntimeException("Glue GetTables: Need catalog id")
        if not name:
            raise dbt.exceptions.RuntimeException("Glue GetTables: Need database name")
        query_params = {"DatabaseName": name, "MaxResults": 50}
        if catalog_id != "awsdatacatalog":
            query_params["CatalogId"] = catalog_id
        # i have no idea adapter's 'database' (tipically "awsdatacatalog") could be an accountid or not.
        logger.debug("Get relations of schema through Glue API: catalog={}, name={}", catalog_id, name)
        conn = self.connections.get_thread_connection()
        client = conn.handle
        glue_client = client.session.client("glue")
        paginator = glue_client.get_paginator("get_tables")
        page_iterator = paginator.paginate(**query_params)
        for page in page_iterator:
            for table in page["TableList"]:
                yield table

    def _create_stats_dict(self, label, value, description, include=True):
        return {
            f"stats:{label}:label": label,
            f"stats:{label}:value": value,
            f"stats:{label}:description": description,
            f"stats:{label}:include": include,
        }

    def _get_rel_type_from_glue_response(self, table):
        if table["TableType"] == "VIRTUAL_VIEW":
            return RelationType.View
        elif table["TableType"] == "EXTERNAL_TABLE":
            return RelationType.Table
        else:
            raise dbt.exceptions.RuntimeException(f'Unknown table type {table["TableType"]} for {table["Name"]}')

    @available
    def quote_seed_column(self, column: str, quote_config: Optional[bool]) -> str:
        return super().quote_seed_column(column, False)

    def _get_catalog_schemas(self, manifest: Manifest) -> AthenaSchemaSearchMap:
        info_schema_name_map = AthenaSchemaSearchMap()
        nodes: Iterator[CompileResultNode] = chain(
            [node for node in manifest.nodes.values() if (node.is_relational and not node.is_ephemeral_model)],
            manifest.sources.values(),
        )
        for node in nodes:
            relation = self.Relation.create_from(self.config, node)
            info_schema_name_map.add(relation)
        return info_schema_name_map
