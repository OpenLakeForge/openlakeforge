# Concepts

The words the rest of the documentation uses, one paragraph each, with a link
to the page that goes deeper.

## Medallion layers

Data moves through three layers, each owned by one tool. **Bronze** holds
source files as CSV in object storage, loaded by dlt. **Silver** holds the
same entities as Iceberg tables, validated by Floe contracts. **Gold** holds
business marts that dbt-trino builds in SQL. Dagster orchestrates the path.
See [Architecture](../architecture/overview.md#bronze-silver-and-gold).

## Sources, domains and data products

A **source** is one system you ingest from, declared in
`lakehouse_code/bronze/<source>/source.yaml`. A **domain** groups the Silver
tables a business area owns. A **data product** belongs to a domain, reads
Silver tables and publishes Gold tables; each product gets one Dagster job,
`<product>_pipeline`. `olf source new`, `olf domain new` and `olf product new`
scaffold them. See [Ingest a source](../tutorial/02-ingest-a-source.md).

## Descriptors

`lakehouse_code/governance.yml` declares domains and products, and each
`source.yaml` declares a source's resources. Descriptors name logical things
only, never Polaris, Glue, S3 or SeaweedFS: OpenLakeForge derives catalog
namespaces and storage paths from them. See the
[descriptor reference](../reference/descriptors.md).

## Deployment Profile

`openlakeforge.yaml` says where the platform runs: the provider (`local`,
`aws`, `azure`), the preset, the enabled stages, access (`spec.access`) and
sign-in (`spec.identity`). `olf profile validate` checks it. See
[Deployment Profile reference](../reference/deployment-profile.md).

## Presets

**Slim**, the default, deploys ingestion, validation, Iceberg, the catalog,
Trino, dbt and Dagster. **Full** adds OpenMetadata for governance and lineage,
and Superset for dashboards. Set `spec.preset: full` and deploy again; the data
path does not change.

## Stages

A profile enables `dev`, an optional `uat`, and `prod`. All stages share one
cluster and the platform services in `olf-system`. Each stage has its own
`olf-<stage>` namespace with its own Dagster (and Superset with Full), its own
buckets and its own `lakehouse_<stage>` catalog. No data is copied between
stages. See [Architecture](../architecture/overview.md#deployment-model).

## Project revisions

`olf infra project build` freezes the project into an immutable **project
revision**: descriptors, contracts, dbt, Dagster code and the project-code
image digest. A revision is activated in DEV, validated, then activated
unchanged in PROD; PROD never rebuilds. See
[CI/CD and environment promotion](../guides/promote-dev-to-prod.md).

## Deploy phases

`olf infra deploy` runs three phases in order. **Foundation** creates the cluster and
registry; **platform** deploys the shared services; **artifacts** deploys
everything derived from `lakehouse_code/`. A code change only reruns
artifacts. See [Architecture](../architecture/overview.md#deployment-model).

## Provider contracts

Platform components talk to infrastructure through provider-neutral
contracts, so local, AWS and Azure expose the same capabilities with
different implementations, such as Polaris or Glue as the catalog. See
[Architecture](../architecture/overview.md#providers).

## Roles

With sign-in on, each person holds one of four canonical roles:
`platform-admin`, `data-engineer`, `analyst` or `viewer`. The profile maps each
role to GitHub teams, and the role decides which services a person can open
and what they can do inside them; an `analyst` reaches Superset but not
Dagster. See [Roles and permissions](../reference/roles-and-permissions.md).
