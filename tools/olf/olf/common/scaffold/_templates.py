"""File-content renderers for `olf source|domain|product new`.

Every renderer produces the exact shape documented in
the tutorial lessons `docs/tutorial/02-*` to `05-*` -- that tutorial is the
specification these functions implement. Plain string templates only (no
Jinja2/ruamel): the repository's dependency footprint stays what it is today.
"""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass

from openlakeforge_services.reports import STAGE_NEUTRAL_SQLALCHEMY_URI

from olf.common.scaffold._csv import InferredColumn
from olf.common.scaffold._shared import yaml_dq

_UUID_NAMESPACE = uuid.UUID("6f2f6c0a-6b0e-4c2a-8f1a-3f7a9e0b6f21")

# Every checked-in Superset report bundle registers the same logical "OpenLakeForge
# Trino" database connection under this one uuid (see
# lakehouse_code/dashboards/superset/*/databases/openlakeforge_trino.yaml). A
# generated bundle must reuse it too, or Superset registers a second, disconnected
# database identity instead of attaching the new datasets to the existing one.
_SUPERSET_TRINO_DATABASE_UUID = "8a87434c-559e-545d-badd-3575affe0185"


def _deterministic_uuid(*parts: str) -> str:
    """A stable uuid5, so repeated generation is byte-identical (no
    `uuid.uuid4()`/randomness in generated artifacts)."""
    return str(uuid.uuid5(_UUID_NAMESPACE, ":".join(parts)))


# --------------------------------------------------------------------------
# Bronze / source
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IncrementalLoad:
    """What `olf source new --kind api|sql` declares for every resource it scaffolds."""

    kind: str  # "api" or "sql"
    cursor: str
    primary_key: str


# The bindings each incremental kind's connector reads (`runtime` in source.yaml).
RUNTIME_KEYS = {
    "api": (("base_url",), ("api_token",)),
    "sql": (("host", "port", "database"), ("username", "password")),
}


def render_source_yaml(
    *,
    source: str,
    display_name: str,
    description: str,
    resources: tuple[tuple[str, str], ...],
    load: IncrementalLoad | None = None,
) -> str:
    lines = [
        "apiVersion: openlakeforge.io/v1beta1",
        "kind: Source",
        f"name: {source}",
        f"displayName: {yaml_dq(display_name)}",
        f"description: {yaml_dq(description)}",
        "status: planned",
        "resources:",
    ]
    for name, resource_description in resources:
        lines.append(f"  - name: {name}")
        lines.append(f"    description: {yaml_dq(resource_description)}")
        if load is not None:
            lines.append(f"    load: {{ mode: incremental, cursor: {load.cursor}, primary_key: [{load.primary_key}] }}")
    if load is not None:
        config, secrets = RUNTIME_KEYS[load.kind]
        lines += ["runtime:", f"  config: [{', '.join(config)}]", f"  secrets: [{', '.join(secrets)}]"]
    return "\n".join(lines) + "\n"


def _cursor_argument(cursor: str) -> str:
    # dlt reads the cursor from the resource's default argument, named after the
    # column; a column named like a builtin (`id`) shadows it on purpose.
    noqa = "A002, B008" if cursor in dir(builtins) else "B008"
    return f'{cursor}: Any = dlt.sources.incremental("{cursor}", initial_value=0),  # noqa: {noqa}'


def render_incremental_connector(
    *, source: str, display_name: str, resources: tuple[str, ...], load: IncrementalLoad
) -> str:
    """A dlt connector on `openlakeforge_runtime.bronze_incremental`: newest-first API pages, or SQL keyset batches."""
    names = ", ".join(f"{resource}_resource" for resource in resources)
    if load.kind == "api":
        resource_blocks = "".join(
            f'''
    @dlt.resource(name="{resource}", primary_key="{load.primary_key}")
    def {resource}_resource(
        {_cursor_argument(load.cursor)}
    ) -> Iterator[Any]:
        yield from newest_first_pages(
            lambda page: fetch_page("{resource}", page),
            {load.cursor},
            cursor_field="{load.cursor}",
            key="{load.primary_key}",
            page_size=_PAGE_SIZE,
            max_pages=max_pages,
            interval_seconds=interval_seconds,
        )
'''
            for resource in resources
        )
        return f'''"""{display_name}: resources read incrementally from an HTTP API.

Scaffolded by `olf source new {source} --kind api`. Adapt `fetch_page` to the
API: it must return one page of items sorted newest first by `{load.cursor}`.
`initial_value=0` sits below every numeric cursor; for an ISO timestamp use a
string such as "1970-01-01T00:00:00Z". The stage binds `base_url` and
`api_token` (see source.yaml `runtime`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import dlt
from dlt.sources.helpers import requests
from openlakeforge_runtime.bronze_csv import BronzeLoadResult
from openlakeforge_runtime.bronze_dlt import load_dlt_source
from openlakeforge_runtime.bronze_incremental import newest_first_pages

_PAGE_SIZE = 100


@dlt.source(name="{source}")
def {source}(
    base_url: str = dlt.config.value,
    api_token: str = dlt.secrets.value,
    max_pages: int = 10,
    interval_seconds: float = 1.0,
) -> Any:
    def fetch_page(path: str, page: int) -> list[Mapping[str, Any]]:
        response = requests.get(
            f"{{base_url.rstrip('/')}}/{{path}}",
            params={{"sort": "-{load.cursor}", "page": page, "per_page": _PAGE_SIZE}},
            headers={{"Authorization": f"Bearer {{api_token}}"}},
        )
        response.raise_for_status()
        return response.json()
{resource_blocks}
    return {names}


def load_{source}_entities_to_bronze(resources: Sequence[str]) -> dict[str, BronzeLoadResult]:
    return load_dlt_source("{source}", {source}(), resources)
'''
    resource_blocks = "".join(
        f'''
    @dlt.resource(name="{resource}", primary_key="{load.primary_key}")
    def {resource}_resource(
        {_cursor_argument(load.cursor)}
    ) -> Iterator[Any]:
        query = sa.text("select * from {resource} where {load.cursor} > :last order by {load.cursor} limit :limit")
        with engine.connect() as connection:
            yield from keyset_batches(
                connection,
                query,
                {load.cursor},
                cursor_field="{load.cursor}",
                batch_size=batch_size,
                max_rows=max_rows_per_run,
            )
'''
        for resource in resources
    )
    return f'''"""{display_name}: tables read incrementally from a database.

Scaffolded by `olf source new {source} --kind sql`. Each resource reads its
table past `{load.cursor}`, an increasing column, at most `max_rows_per_run`
rows a run. Adapt the queries and the driver (`postgresql+psycopg2` here). The
stage binds `host`, `port`, `database`, `username` and `password` (see
source.yaml `runtime`).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import dlt
import sqlalchemy as sa
from openlakeforge_runtime.bronze_csv import BronzeLoadResult
from openlakeforge_runtime.bronze_dlt import load_dlt_source
from openlakeforge_runtime.bronze_incremental import keyset_batches, sql_engine


@dlt.source(name="{source}")
def {source}(
    host: str = dlt.config.value,
    port: int = dlt.config.value,
    database: str = dlt.config.value,
    username: str = dlt.secrets.value,
    password: str = dlt.secrets.value,
    batch_size: int = 10_000,
    max_rows_per_run: int = 100_000,
) -> Any:
    binding = {{"host": host, "port": port, "database": database, "username": username, "password": password}}
    engine = sql_engine("postgresql+psycopg2", binding, connect_args={{"connect_timeout": 30}})
{resource_blocks}
    return {names}


def load_{source}_entities_to_bronze(resources: Sequence[str]) -> dict[str, BronzeLoadResult]:
    return load_dlt_source("{source}", {source}(), resources)
'''


def incremental_example_csv(load: IncrementalLoad) -> str:
    """A one-row example whose columns seed the Silver contract `olf domain new` infers."""
    columns = dict.fromkeys((load.primary_key, load.cursor, "name"))
    values = {load.primary_key: "1", load.cursor: "1700000000", "name": "example"}
    return ",".join(columns) + "\n" + ",".join(values[column] for column in columns) + "\n"


def render_dlt_loader(*, source: str, resources: tuple[str, ...]) -> str:
    entities_tuple = ", ".join(f'"{name}"' for name in resources)
    if len(resources) == 1:
        entities_tuple += ","
    return f'''from __future__ import annotations

from pathlib import Path

from openlakeforge_runtime.bronze_csv import BronzeLoadResult, load_entities_to_bronze

{source.upper()}_ENTITIES = ({entities_tuple})

_SOURCE_DIR = Path(__file__).resolve().parents[1]
_RAW_DIR = _SOURCE_DIR / "examples"
_BRONZE_PREFIX = "{source}"


def load_{source}_entities_to_bronze(
    entities: tuple[str, ...],
    raw_dir: Path | None = None,
) -> dict[str, BronzeLoadResult]:
    """Load a subset of {source} resources into Bronze under the ``{source}`` prefix."""
    return load_entities_to_bronze(
        entities=entities,
        raw_dir=raw_dir or _RAW_DIR,
        bronze_prefix=_BRONZE_PREFIX,
    )


def load_all_entities_to_bronze(raw_dir: Path | None = None) -> dict[str, BronzeLoadResult]:
    """Load every resource declared in ``bronze/{source}/source.yaml``."""
    return load_{source}_entities_to_bronze({source.upper()}_ENTITIES, raw_dir=raw_dir)
'''


def render_bronze_readme(*, source: str, display_name: str, kind: str = "csv") -> str:
    if kind != "csv":
        return f"""# {display_name}

Scaffolded by `olf source new {source} --kind {kind}`.

`dlt/{source}.py` reads each resource incrementally through
`openlakeforge_runtime.bronze_incremental`; adapt its request or queries to the real source.
The example CSV(s) under `examples/` are not loaded: they only seed the
columns `olf domain new` writes into the Silver contract, so give them the
real columns first. Each stage binds the keys `source.yaml` lists under
`runtime`. See [Load an external source
incrementally](../../../docs/guides/incremental-sources.md).
"""
    return f"""# {display_name}

Scaffolded by `olf source new {source}`.

Replace the placeholder example CSV(s) under `examples/` with real sample
data for each resource, and point `dlt/{source}.py` at a real ingestion
adapter when this source moves beyond local CSV examples. See
[Ingest a source](../../../docs/tutorial/02-ingest-a-source.md)
for the full ownership model this source participates in.
"""


# --------------------------------------------------------------------------
# Silver / domain
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FloeEntitySpec:
    table: str
    source: str
    resource: str
    domain: str
    columns: tuple[InferredColumn, ...]
    # From the resource's `load` in source.yaml; empty for a `full` resource.
    primary_key: tuple[str, ...] = ()
    incremental: bool = False


def render_domain_readme(*, domain: str, display_name: str) -> str:
    return f"""# {display_name}

Scaffolded by `olf domain new {domain}`.

This domain owns the `{domain}_silver` Iceberg namespace and its Floe
Bronze-to-Silver contracts under `contracts/floe/`: `_domain.yml` holds the
sink defaults and each sibling `.yml` file is one entity. Every product
built on `{domain}` shares this one Silver namespace -- see
[ADR 0004](../../../docs/adr/0004-medallion-layout-and-catalog-namespaces.md).
"""


def _floe_column_lines(columns: tuple[InferredColumn, ...], primary_key: tuple[str, ...] = ()) -> str:
    if not columns:
        return (
            "  # TODO: no example CSV was available to infer columns from;\n"
            "  # declare this entity's columns by hand.\n"
            "  columns: []\n"
        )
    rendered = "\n".join(
        f'    - {{ name: {yaml_dq(column.name)}, type: "{column.type}", nullable: {str(column.nullable).lower()} }}'
        for column in columns
    )
    keys = ", ".join(yaml_dq(key) for key in primary_key or (columns[0].name,))
    return f"  primary_key: [{keys}]\n  columns:\n{rendered}\n"


def render_floe_entity(spec: FloeEntitySpec) -> str:
    """One entity file beside its domain's `_domain.yml`, which supplies every sink default."""
    # An incremental resource lands only new rows: read the new files, merge on the key.
    mode = 'incremental_mode: "file"\nsink: { write_mode: "merge_scd1" }\n' if spec.incremental else ""
    return f'''name: "{spec.table}"
source: {{ ref: "{spec.source}", resource: "{spec.resource}" }}
{mode}schema:
{_floe_column_lines(spec.columns, spec.primary_key)}'''


def render_floe_domain(*, domain: str) -> str:
    return f'''name: "{domain}"
defaults:
  incremental_mode: "none"
  sink:
    write_mode: "overwrite"
    accepted:
      format: "iceberg"
      path: "{domain}/{{{{entity.name}}}}"
      storage: "lakehouse_silver"
      iceberg: {{ catalog: "iceberg_catalog", namespace: "{domain}_silver" }}
    rejected: {{ format: "csv", path: "floe/rejected/{domain}/{{{{entity.name}}}}", storage: "lakehouse_silver" }}
  policy: {{ severity: "reject" }}
  schema:
    normalize_columns: {{ enabled: true, strategy: "snake_case" }}
'''


def render_floe_source(*, source: str, parquet: bool = False) -> str:
    """A `sources.yml` entry for the files a Bronze source lands under `<source>/<resource>`.

    CSV for the example loader; Parquet for a dlt connector, whose glob leaves
    dlt's own `_dlt_*` bookkeeping beside the resources out.
    """
    if parquet:
        return f'''  - name: "{source}"
    format: "parquet"
    path: "{source}/{{{{resource}}}}"
    storage: "lakehouse_bronze"
    options: {{ glob: "*.parquet" }}
'''
    return f'''  - name: "{source}"
    format: "csv"
    path: "{source}/{{{{resource}}}}"
    storage: "lakehouse_bronze"
    options: {{ header: true, separator: ",", encoding: "utf-8", glob: "*.csv" }}
    cast_mode: "strict"
'''


# --------------------------------------------------------------------------
# Gold / product
# --------------------------------------------------------------------------


def render_dbt_project_yml(*, domain: str, product: str) -> str:
    return f'''name: {product}
version: "1.0.0"
config-version: 2

profile: {product}

model-paths: ["models"]
macro-paths: []
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  {product}:
    +database: "{{{{ env_var('OPENLAKEFORGE_CATALOG_NAME') }}}}"
    +materialized: table
    +on_table_exists: replace
    +meta:
      dagster:
        group: {product}
    gold:
      +tags: ["{domain}", "{product}", "gold"]
'''


def render_dbt_packages_yml() -> str:
    # The package ships with the distribution, not the project; olf and the
    # project-code image set the variable to where it is installed (#627).
    return "packages:\n  - local: \"{{ env_var('OPENLAKEFORGE_DBT_PACKAGES_DIR') }}/openlakeforge_dbt\"\n"


def render_dbt_sources_yml(*, domain: str, tables: tuple[tuple[str, str], ...]) -> str:
    lines = [
        "version: 2",
        "",
        "sources:",
        "  - name: silver",
        "    database: \"{{ env_var('OPENLAKEFORGE_CATALOG_NAME') }}\"",
        f"    schema: \"{{{{ env_var('OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX', '') }}}}{domain}_silver\"",
        f"    description: Floe-owned {domain.replace('_', ' ').title()} Silver Iceberg tables.",
        "    tables:",
    ]
    for name, description in tables:
        lines.append(f"      - name: {name}")
        lines.append(f"        description: {description}")
    return "\n".join(lines) + "\n"


def render_gold_mart_sql(*, mart: str, first_silver_input: str) -> str:
    return f"""-- TODO: replace this starter model with real aggregation logic.
select *
from {{{{ source('silver', '{first_silver_input}') }}}}
"""


def render_gold_schema_yml(*, gold_tables: tuple[tuple[str, str], ...]) -> str:
    lines = ["version: 2", "", "models:"]
    for name, description in gold_tables:
        lines.append(f"  - name: {name}")
        lines.append(f"    description: {description}")
    return "\n".join(lines) + "\n"


def render_dagster_product_module(*, domain: str, product: str) -> str:
    return f'''from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions

# No input lists: the dbt and Floe projects already declare them, and a second copy drifts.
# Pass gold_assets, silver_inputs or bronze_inputs only to override what they declare.
defs = build_product_definitions(ProductDefinitionSpec(domain="{domain}", product="{product}"))
'''


# --------------------------------------------------------------------------
# Dashboards / Superset (--with-report)
# --------------------------------------------------------------------------


def render_superset_database_yaml() -> str:
    return f"""database_name: OpenLakeForge Trino
sqlalchemy_uri: {STAGE_NEUTRAL_SQLALCHEMY_URI}
cache_timeout: null
expose_in_sqllab: true
allow_run_async: false
allow_ctas: false
allow_cvas: false
allow_dml: false
allow_file_upload: false
extra:
  metadata_params: {{}}
  engine_params: {{}}
  metadata_cache_timeout: {{}}
  per_user_caching: true
  schemas_allowed_for_file_upload: []
impersonate_user: true
uuid: {_SUPERSET_TRINO_DATABASE_UUID}
version: 1.0.0
"""


def render_superset_dataset_yaml(*, dashboard: str, mart: str, gold_namespace: str, description: str) -> str:
    dataset_uuid = _deterministic_uuid("superset-dataset", dashboard, mart)
    return f"""table_name: {mart}
main_dttm_col: null
description: {yaml_dq(description)}
schema: {gold_namespace}
uuid: {dataset_uuid}
metrics: []
columns: []
version: 1.0.0
database_uuid: {_SUPERSET_TRINO_DATABASE_UUID}
"""


def render_superset_readme(*, display_name: str, report_source_dir: str) -> str:
    return f"""# {display_name} Superset Assets

Scaffolded by `olf product new --with-report`. This bundle registers the
Gold mart dataset(s) and a Trino database connection; it does not include a
dashboard layout or charts, which the scaffold cannot generate meaningfully.

This directory is a draft, not yet a promotable bundle: it has no
`metadata.yaml`, so `olf infra project build` and `olf report validate` both ignore
it until you finish this step.

Build the dashboard in Superset, then export it back into this directory.
`olf infra report export` defaults to the first dashboard it finds, so target this
one explicitly with `SUPERSET_REPORT_SOURCE_DIR` --
otherwise it silently re-exports into an unrelated, already-checked-in bundle
instead of this one:

```bash
SUPERSET_REPORT_SOURCE_DIR={report_source_dir} \\
SUPERSET_DASHBOARD="<the dashboard's slug or uuid in Superset>" \\
uv run --project tools/olf olf infra report export --stage dev
```

The export writes this bundle's `metadata.yaml`. Later re-exports find the
dashboard by the uuid it wrote, so they need no `SUPERSET_DASHBOARD` and
survive renaming the dashboard in Superset. Once `metadata.yaml` exists, this
bundle is validated and promoted exactly like any other Superset dashboard.
"""
