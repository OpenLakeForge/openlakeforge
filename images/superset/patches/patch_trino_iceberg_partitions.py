from pathlib import Path


TRINO_ENGINE_SPEC = Path("/app/superset/db_engine_specs/trino.py")

source = TRINO_ENGINE_SPEC.read_text()

class_marker = """\
    allows_alias_to_source_column = False
"""

class_patch = """\
    allows_alias_to_source_column = False
    iceberg_partition_metadata_columns = {"data", "file_count", "record_count", "total_size"}

    @classmethod
    def _is_iceberg_database(cls, database: Database) -> bool:
        # The people database (ADR 0019 section 9) names the stage catalog,
        # which Superset's config lists beside the shared `iceberg` one.
        catalogs = app.config.get("OLF_ICEBERG_CATALOGS", ["iceberg"])
        return str(database.sqlalchemy_uri).rstrip("/").rsplit("/", 1)[-1] in catalogs

    @classmethod
    def _filter_iceberg_partition_indexes(
        cls,
        database: Database,
        indexes: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if not cls._is_iceberg_database(database):
            return indexes or []

        filtered_indexes = []
        for index in indexes or []:
            if index.get("name") != "partition":
                filtered_indexes.append(index)
                continue

            column_names = [
                column_name
                for column_name in index.get("column_names", [])
                if column_name not in cls.iceberg_partition_metadata_columns
            ]
            if column_names:
                filtered_indexes.append({**index, "column_names": column_names})

        return filtered_indexes

    @classmethod
    def latest_partition(
        cls,
        database: Database,
        table: Table,
        show_first: bool = False,
        indexes: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], list[str] | None]:
        return super().latest_partition(
            database=database,
            table=table,
            show_first=show_first,
            indexes=cls._filter_iceberg_partition_indexes(
                database,
                indexes if indexes is not None else database.get_indexes(table),
            ),
        )

    # OAuth 2.0
"""

metadata_marker = """\
        if indexes := database.get_indexes(table):
"""

metadata_patch = """\
        if indexes := cls._filter_iceberg_partition_indexes(
            database,
            database.get_indexes(table),
        ):
"""

if class_marker not in source:
    raise SystemExit("Could not find TrinoEngineSpec class marker")

if metadata_marker not in source:
    raise SystemExit("Could not find Trino get_extra_table_metadata index marker")

source = source.replace(class_marker, class_patch, 1)
source = source.replace(metadata_marker, metadata_patch, 1)

TRINO_ENGINE_SPEC.write_text(source)
