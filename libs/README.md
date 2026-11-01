# Shared Libraries

`libs/` is reserved for reusable OpenLakeForge platform glue. It must not contain domain business logic.

Appropriate examples include:

- config loading
- shared dbt packages, macros, and environment profile templates
- the Floe environment profile rendered for the pod's stage
- storage path conventions
- Dagster resource helpers
- OpenLineage naming conventions
- logging and observability helpers

Business logic stays under `lakehouse_code/` (bronze/silver/gold/dashboards/
pipelines); `libs/` contains reusable platform glue shared across that code.

The provider-neutral descriptor and inventory model is deliberately not in
`libs/`: it is the `openlakeforge_domain` package under `packages/domain-model/`
so `olf` and project-code install the identical implementation.

The dbt profile template lives under `libs/dbt/profiles/`, one file per query
engine (`trino.yml` today). `libs.dbt.render_profiles` renders it into
`generated/dbt-profiles/` at image build. Every stage value in it is an
`env_var()` reference, never a default. `olf check project-code` renders the
checked project's own profiles into its cache and points the code location at
them with `OPENLAKEFORGE_DBT_PROFILES_DIR`. Product dbt projects keep
product-local model code and schema defaults.

Each product's `packages.yml` reaches the shared `openlakeforge_dbt` package
as `{{ env_var('OPENLAKEFORGE_DBT_PACKAGES_DIR') }}/openlakeforge_dbt`. A
path relative to the product would resolve only where `libs/` sits beside
`lakehouse_code/`, which a customer repository does not have. The image sets
the variable to `/opt/openlakeforge/libs/dbt`; `olf check dbt`,
`olf check project-code` and `olf infra dbt parse` set it to the
distribution's `libs/dbt`.
