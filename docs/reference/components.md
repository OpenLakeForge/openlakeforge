# Components and services

The open-source components OpenLakeForge deploys, the version this release
pins, which providers run each one, and the URL each user-facing service is
published at. Exact image digests, Helm charts and Terraform providers are in
the [Compatibility matrix](compatibility-matrix.md).

## Platform services

| Component | Role | Version | Local | AWS | Azure |
| --- | --- | --- | --- | --- | --- |
| [Dagster](https://dagster.io/) | Orchestration: one asset graph and one job per data product, one instance per stage | 1.13.7 | yes | yes | yes |
| [Trino](https://trino.io/) | SQL engine over the Iceberg catalog; dbt builds Gold through it | 480 | yes | yes | yes |
| [Apache Polaris](https://polaris.apache.org/) | Iceberg REST catalog | 1.4.0 | yes | AWS Glue instead | yes |
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | S3-compatible object storage for Bronze, Silver, Gold and run artifacts | 4.23 | yes | Amazon S3 instead | yes |
| [PostgreSQL](https://www.postgresql.org/) | Metadata databases of the platform services | 16 | yes | Amazon RDS instead | yes |
| [Apache Superset](https://superset.apache.org/) | Dashboards over Gold tables, one instance per stage with `analytics` on | 6.1.0 | Full | Full | Full |
| [OpenMetadata](https://open-metadata.org/) | Governance: domains, data products and lineage, with an [OpenSearch](https://opensearch.org/) 3.3.2 index | 1.13.6 | Full | Full | Full |
| [Traefik](https://traefik.io/) and [cert-manager](https://cert-manager.io/) | Ingress and route certificates | v3.7.13, v1.21.2 | with `spec.access` | no | no |
| [Dex](https://dexidp.io/) and [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) | GitHub sign-in and the role-checking perimeter | v2.46.0, v7.15.5 | with `spec.identity` | no | no |

## Data tooling

These run your pipelines from the `project-code` image built from
`lakehouse_code/`.

| Component | Role | Version |
| --- | --- | --- |
| [dlt](https://dlthub.com/) | Extracts a source into Bronze | 1.29.0 |
| [Floe](https://github.com/malon64/floe) | Validates Bronze against a domain contract and writes Silver Iceberg tables; rejected rows are quarantined | `floe-python` 0.8.4, `dagster-floe` 0.4.2 (in the project-code image) |
| [dbt](https://www.getdbt.com/) with dbt-trino | Builds Gold models on Trino | dbt-core 1.11.12, dbt-trino 1.10.2 |
| [Apache Iceberg](https://iceberg.apache.org/) | Table format of Silver and Gold | |

## Service URLs

Routes exist on the local provider when the Deployment Profile declares
`spec.access`, which `olf init` does. `<base>` is `spec.access.base_domain`,
`olf.localhost` by default. The scheme is `http` with `issuer: none` and
`https` otherwise.

| Service | URL | Present when |
| --- | --- | --- |
| Portal | `<scheme>://<base>` | always |
| Dagster | `<scheme>://dagster-<stage>.<base>` | one per enabled stage |
| Superset | `<scheme>://superset-<stage>.<base>` | one per stage with `analytics` on |
| Trino UI | `<scheme>://trino.<base>` | always |
| OpenMetadata | `<scheme>://openmetadata.<base>` | a stage has `governance` on |
| Sign-in (Dex) | `<scheme>://auth.<base>` | `spec.identity.issuer` is `github` |

Polaris, SeaweedFS, the databases and code servers are never published; reach
them with `olf forward`. On AWS and Azure every service is reached with
`olf forward`. Which role may open each service is in
[Roles and permissions](roles-and-permissions.md).
