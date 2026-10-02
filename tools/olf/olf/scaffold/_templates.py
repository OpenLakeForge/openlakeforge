"""File-content renderers for `olf source|domain|product new`.

Every renderer produces the exact shape documented in
`docs/getting-started/first-data-product.md` -- that tutorial is the
specification these functions implement. Plain string templates only (no
Jinja2/ruamel): the repository's dependency footprint stays what it is today.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from olf.scaffold._csv import InferredColumn
from olf.scaffold._shared import yaml_dq

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


def render_source_yaml(
    *, source: str, display_name: str, description: str, resources: tuple[tuple[str, str], ...]
) -> str:
    lines = [
        "apiVersion: openlakeforge.io/v1alpha3",
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
    return "\n".join(lines) + "\n"


def render_dlt_loader(*, source: str, resources: tuple[str, ...]) -> str:
    entities_tuple = ", ".join(f'"{name}"' for name in resources)
    if len(resources) == 1:
        entities_tuple += ","
    return f'''from __future__ import annotations

from pathlib import Path

from libs.bronze_csv import BronzeLoadResult, load_entities_to_bronze

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


def render_bronze_readme(*, source: str, display_name: str) -> str:
    return f"""# {display_name}

Scaffolded by `olf source new {source}`.

Replace the placeholder example CSV(s) under `examples/` with real sample
data for each resource, and point `dlt/{source}.py` at a real ingestion
adapter when this source moves beyond local CSV examples. See
[Build your first data product](../../../docs/getting-started/first-data-product.md)
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


def render_domain_readme(*, domain: str, display_name: str) -> str:
    return f"""# {display_name}

Scaffolded by `olf domain new {domain}`.

This domain owns the `{domain}_silver` Iceberg namespace and its Floe
Bronze-to-Silver contract at `contracts/floe/{domain}.yml`. Every product
built on `{domain}` shares this one Silver namespace and this one Floe
contract -- see
[ADR 0004](../../../docs/adr/0004-medallion-layout-and-catalog-namespaces.md).
"""


def _floe_column_lines(columns: tuple[InferredColumn, ...]) -> str:
    if not columns:
        return (
            "      # TODO: no example CSV was available to infer columns from;\n"
            "      # declare this entity's columns by hand.\n"
            "      columns: []\n"
        )
    rendered = "\n".join(
        f"        - {{ name: {yaml_dq(column.name)}, type: \"{column.type}\", "
        f"nullable: {str(column.nullable).lower()} }}"
        for column in columns
    )
    primary_key = columns[0].name
    return f"      primary_key: [{yaml_dq(primary_key)}]\n      columns:\n{rendered}\n"


def render_floe_entity(spec: FloeEntitySpec) -> str:
    return f'''  - name: "{spec.table}"
    incremental_mode: "none"
    source:
      format: "csv"
      path: "{spec.source}/{spec.resource}"
      storage: "lakehouse_bronze"
      options: {{ header: true, separator: ",", encoding: "utf-8", glob: "*.csv" }}
      cast_mode: "strict"
    sink:
      write_mode: "overwrite"
      accepted:
        format: "iceberg"
        path: "{spec.domain}/{spec.table}"
        storage: "lakehouse_silver"
        iceberg: {{ catalog: "iceberg_catalog", namespace: "{spec.domain}_silver", table: "{spec.table}" }}
      rejected: {{ format: "csv", path: "floe/rejected/{spec.domain}/{spec.table}", storage: "lakehouse_silver" }}
    policy: {{ severity: "reject" }}
    schema:
      normalize_columns: {{ enabled: true, strategy: "snake_case" }}
{_floe_column_lines(spec.columns)}'''


def render_floe_contract(*, domain: str, entities: tuple[str, ...]) -> str:
    header = f'''version: "0.2"

metadata:
  project: "openlakeforge"
  owner: "{domain}"
  description: "{domain.replace('_', ' ').title()} Bronze to Silver Floe contracts."
  tags: ["{domain}", "silver"]

storages:
  default: "lakehouse_bronze"
  definitions:
    - name: "lakehouse_bronze"
      type: "s3"
      bucket: "{{{{OPENLAKEFORGE_STORAGE_BRONZE_BUCKET}}}}"
      region: "{{{{OPENLAKEFORGE_STORAGE_REGION}}}}"
    - name: "lakehouse_silver"
      type: "s3"
      bucket: "{{{{OPENLAKEFORGE_STORAGE_SILVER_BUCKET}}}}"
      region: "{{{{OPENLAKEFORGE_STORAGE_REGION}}}}"
    - name: "openlakeforge_ops"
      type: "s3"
      bucket: "{{{{OPENLAKEFORGE_OPS_BUCKET_NAME}}}}"
      region: "{{{{OPENLAKEFORGE_STORAGE_REGION}}}}"

report:
  path: "floe/reports/{domain}"
  storage: "openlakeforge_ops"

entities:
'''
    return header + "\n\n".join(entities) + "\n"


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
    +database: "{{{{ env_var('OPENLAKEFORGE_CATALOG_NAME', 'lakehouse_dev') }}}}"
    +materialized: table
    +on_table_exists: replace
    +meta:
      dagster:
        group: {product}
    gold:
      +tags: ["{domain}", "{product}", "gold"]
'''


def render_dbt_packages_yml() -> str:
    return "packages:\n  - local: ../../../../libs/dbt/openlakeforge_dbt\n"


def render_dbt_sources_yml(*, domain: str, tables: tuple[tuple[str, str], ...]) -> str:
    lines = [
        "version: 2",
        "",
        "sources:",
        "  - name: silver",
        "    database: \"{{ env_var('OPENLAKEFORGE_CATALOG_NAME', 'lakehouse_dev') }}\"",
        f"    schema: {domain}_silver",
        f"    description: Floe-owned {domain.replace('_', ' ').title()} Silver Iceberg tables.",
        "    tables:",
    ]
    for name, description in tables:
        lines.append(f"      - name: {name}")
        lines.append(f"        description: {description}")
    return "\n".join(lines) + "\n"


def render_gold_mart_sql(*, mart: str, first_silver_input: str) -> str:
    return f'''-- TODO: replace this starter model with real aggregation logic.
select *
from {{{{ source('silver', '{first_silver_input}') }}}}
'''


def render_gold_schema_yml(*, gold_tables: tuple[tuple[str, str], ...]) -> str:
    lines = ["version: 2", "", "models:"]
    for name, description in gold_tables:
        lines.append(f"  - name: {name}")
        lines.append(f"    description: {description}")
    return "\n".join(lines) + "\n"


def render_dagster_product_module(
    *,
    domain: str,
    product: str,
    silver_inputs: tuple[str, ...],
    bronze_inputs: tuple[tuple[str, str], ...],
    gold_assets: tuple[str, ...],
) -> str:
    const_prefix = product.upper()
    silver_tuple = ", ".join(f'"{name}"' for name in silver_inputs)
    if len(silver_inputs) == 1:
        silver_tuple += ","
    bronze_tuple = ", ".join(f'("{source}", "{resource}")' for source, resource in bronze_inputs)
    if len(bronze_inputs) == 1:
        bronze_tuple += ","
    gold_tuple = ", ".join(f'"{name}"' for name in gold_assets)
    if len(gold_assets) == 1:
        gold_tuple += ","
    return f'''from __future__ import annotations

from libs.product_dagster import ProductDefinitionSpec, build_product_definitions

{const_prefix}_SILVER_INPUTS = ({silver_tuple})

{const_prefix}_GOLD_ASSETS = ({gold_tuple})

defs = build_product_definitions(
    ProductDefinitionSpec(
        domain="{domain}",
        product="{product}",
        silver_inputs={const_prefix}_SILVER_INPUTS,
        bronze_inputs=({bronze_tuple}),
        gold_assets={const_prefix}_GOLD_ASSETS,
    )
)
'''


# --------------------------------------------------------------------------
# Dashboards / Superset (--with-report)
# --------------------------------------------------------------------------


def render_superset_database_yaml() -> str:
    return f'''database_name: OpenLakeForge Trino
sqlalchemy_uri: trino://superset@trino:8080/iceberg
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
  schemas_allowed_for_file_upload: []
impersonate_user: false
uuid: {_SUPERSET_TRINO_DATABASE_UUID}
version: 1.0.0
'''


def render_superset_dataset_yaml(*, dashboard: str, mart: str, gold_namespace: str, description: str) -> str:
    dataset_uuid = _deterministic_uuid("superset-dataset", dashboard, mart)
    return f'''table_name: {mart}
main_dttm_col: null
description: {yaml_dq(description)}
schema: {gold_namespace}
uuid: {dataset_uuid}
metrics: []
columns: []
version: 1.0.0
database_uuid: {_SUPERSET_TRINO_DATABASE_UUID}
'''


def render_superset_readme(*, display_name: str, dashboard_name: str, report_source_dir: str) -> str:
    return f"""# {display_name} Superset Assets

Scaffolded by `olf product new --with-report`. This bundle registers the
Gold mart dataset(s) and a Trino database connection; it does not include a
dashboard layout or charts, which the scaffold cannot generate meaningfully.

This directory is a draft, not yet a promotable bundle: it has no
`metadata.yaml` and `lakehouse.yaml` does not declare it, so `olf project
build` and `olf report validate` both ignore it until you finish these two
steps.

1. Build the dashboard in Superset, then export it back into this directory.
   `olf report export` defaults to the lakehouse's *first* declared dashboard,
   so target this one explicitly with `SUPERSET_REPORT_SOURCE_DIR` --
   otherwise it silently re-exports into an unrelated, already-checked-in
   bundle instead of this one:

   ```bash
   SUPERSET_REPORT_SOURCE_DIR={report_source_dir} \\
   SUPERSET_DASHBOARD="<the dashboard's slug or uuid in Superset>" \\
   uv run --project tools/olf olf report export --stage dev
   ```

   The export writes this bundle's `metadata.yaml`. Later re-exports find
   the dashboard by the uuid it wrote, so they need no `SUPERSET_DASHBOARD`
   and survive renaming the dashboard in Superset.

2. Declare the dashboard by adding it to `lakehouse_code/lakehouse.yaml`'s
   `dashboards:` list:

   ```yaml
     - name: {dashboard_name}
       products: [{dashboard_name}]
   ```

Once both are done, this bundle is validated and promoted exactly like any
other declared Superset dashboard.
"""
