# Shared Libraries

`openlakeforge_runtime/` is reserved for reusable OpenLakeForge platform glue. It must not contain domain business logic.

Appropriate examples include:

- config loading
- shared dbt packages, macros, and environment profile templates
- the Floe environment profile rendered for the pod's stage
- storage path conventions
- Dagster resource helpers
- OpenLineage naming conventions
- logging and observability helpers
- Bronze loading: `bronze_dlt` runs a dlt source as `source.yaml` declares,
  and `bronze_incremental` holds the read patterns incremental connectors share

Business logic stays under `lakehouse_code/` (bronze/silver/gold/dashboards/
pipelines); `openlakeforge_runtime/` contains reusable platform glue shared across that code.

The provider-neutral descriptor and inventory model is deliberately not in
`openlakeforge_runtime/`: it is the `openlakeforge_core` package under `packages/core/`
so `olf` and project-code install the identical implementation.

The dbt profile template lives under `openlakeforge_runtime/dbt/profiles/`, one file per query
engine (`trino.yml` today). `openlakeforge_runtime.dbt.render_profiles` renders it into
`generated/dbt-profiles/` at image build. Every stage value in it is an
`env_var()` reference, never a default. `olf check project-code` renders the
checked project's own profile into its cache and points the code location at
it with `OPENLAKEFORGE_DBT_PROFILES_DIR`. The Gold dbt project holds a folder
of models per product. `openlakeforge_dbt`'s `generate_schema_name` names each
model's schema after its folder (ADR 0004); dbt takes that macro only from the
root project, so the project's `macros/generate_schema_name.sql` delegates to
it.

The Gold project's `packages.yml` reaches the shared `openlakeforge_dbt` package
as `{{ env_var('OPENLAKEFORGE_DBT_PACKAGES_DIR') }}/openlakeforge_dbt`. A
path relative to the project would resolve only where `openlakeforge_runtime/` sits beside
`lakehouse_code/`, which a customer repository does not have. The installed
runtime sets the variable to its own `dbt/` directory (`render_profiles.dbt_environment()`,
applied when `product_dagster` is imported); `olf check dbt`,
`olf check project-code` and `olf infra dbt parse` set it the same way.
