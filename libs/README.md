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
`env_var()` reference, never a default. Product dbt projects keep
product-local model code and schema defaults.
