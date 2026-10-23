# Architecture

How OpenLakeForge is put together: the services it runs, how data moves from
Bronze to Gold, how one pipeline run uses them, and what changes between the
local, AWS and Azure providers.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/architecture-local-dark.svg">
  <img alt="OpenLakeForge architecture: Dagster orchestrates dlt, Floe and dbt-trino over Bronze, Silver and Gold Iceberg tables in object storage, queried by Trino and Superset and governed by OpenMetadata" src="../assets/architecture-local-light.svg">
</picture>

## Services and responsibilities

Everything runs on one Kubernetes cluster that `olf deploy` creates.

| Concern | Service | Responsibility |
| --- | --- | --- |
| Orchestration | Dagster | Runs one job per data product and records every run |
| Ingestion | dlt | Lands source data in Bronze |
| Validation | Floe | Checks Bronze against the domain's contract and writes Silver |
| Transformation | dbt-trino | Builds Gold marts in SQL |
| Compute | Trino | Runs queries and the dbt SQL; the only query engine |
| Table format | Apache Iceberg | Silver and Gold tables |
| Catalog | Polaris, or AWS Glue | Registers every Iceberg table |
| Storage | SeaweedFS, or Amazon S3 | Holds Bronze files, Iceberg data and run artifacts |
| Service state | PostgreSQL, or Amazon RDS | Databases of Dagster, Superset, OpenMetadata and Polaris |
| Governance (Full) | OpenMetadata | Domains, data products and lineage |
| Analytics (Full) | Superset | Dashboards over Gold tables |

Component versions are in [Components and services](../reference/components.md).

## Bronze, Silver and Gold

Each layer has one owner in your project and one tool that writes it:

| Layer | Owned by | Written by | Holds | Namespace |
| --- | --- | --- | --- | --- |
| Bronze | A source | dlt | Source files as they arrived | `<source>_bronze` |
| Silver | A domain | Floe | Validated Iceberg tables; rejected rows go to a quarantine path instead | `<domain>_silver` |
| Gold | A data product | dbt-trino | Business marts | `<product>_gold` |

dbt reads the Silver tables Floe writes and keeps no staging layer of its own.
Every Silver and Gold write is committed through the catalog, so Trino,
Superset and OpenMetadata see the same tables.

## One pipeline run

When you launch `<product>_pipeline` in Dagster:

1. Dagster starts a short-lived run pod from your project-code image.
2. The run pod loads each Bronze resource with dlt.
3. For each Silver table, it starts a Floe job, one after another. Floe writes
   accepted rows to Silver and a report to the ops bucket.
4. dbt-trino sends the Gold SQL to Trino, which does the work and writes the
   Gold tables.
5. Floe and dbt emit OpenLineage events to OpenMetadata on Full, so lineage
   comes from the runs themselves.

Run and Floe pods are removed after the run, so nothing computes between runs
except the long-lived services. Logs, Floe reports and dbt artifacts are
archived in the ops bucket.

## Deployment model

`olf deploy` runs three phases:

| Phase | Creates | Changes when |
| --- | --- | --- |
| Foundation | The cluster and image registry | Rarely |
| Platform | The shared services and each stage | You change the preset or stages |
| Artifacts | Your code: image, catalog namespaces, Floe manifests, Dagster code, dashboards, metadata | You change `lakehouse_code/` |

A code change only reruns artifacts and never touches Terraform. Shared
services run once in the `olf-system` namespace. Each enabled stage (`dev`,
`uat`, `prod`) gets its own `olf-<stage>` namespace with its own Dagster and
Superset, its own buckets and its own catalog `lakehouse_<stage>`. A change is
built once into an immutable project revision and promoted between stages
unchanged; see [CI/CD and environment promotion](../guides/promote-dev-to-prod.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/stage-promotion-dark.svg">
  <img alt="Stage promotion: a merged pull request builds one image and one project revision, which is activated in the DEV namespace, tested, approved, and then activated unchanged in PROD" src="../assets/stage-promotion-light.svg">
</picture>

## Providers

The platform components talk to infrastructure through provider-neutral
contracts, so your project and its descriptors are identical on every
provider:

| | Local | AWS | Azure (preview) |
| --- | --- | --- | --- |
| Kubernetes | kind on Docker | EKS | AKS |
| Images | Loaded into kind | ECR | ACR |
| Storage | SeaweedFS | S3 | SeaweedFS |
| Catalog | Polaris | AWS Glue | Polaris |
| Service state | PostgreSQL | RDS PostgreSQL | PostgreSQL |
| Workload identity | Kubernetes service accounts | EKS Pod Identity | Kubernetes service accounts |
| Access | Ingress on `*.olf.localhost`, optional sign-in | `olf forward` | `olf forward` |

On AWS, every stage shares the account's Glue catalog, with stage-prefixed
database names scoped by IAM.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/architecture-aws-dark.svg">
  <img alt="OpenLakeForge on AWS: Dagster on EKS orchestrates dlt, Floe and dbt-trino over per-stage S3 buckets registered in the Glue Data Catalog, with RDS PostgreSQL for service state, ECR for images, and Route 53, an ALB with ACM, oauth2-proxy and Dex for sign-in" src="../assets/architecture-aws-light.svg">
</picture>

The AWS chart shows the target shape. Its access path (Route 53, an ALB with
ACM, oauth2-proxy and Dex) is not built yet: today the AWS services are reached
with `olf forward`.

## Further reading

- [Kubernetes runtime charts](diagrams.md): every pod, the service
  wiring, the jobs of one pipeline run, the data path and the provider
  contracts.
- [Architecture Decision Records](../adr/README.md): the decisions behind this
  design, with their trade-offs.
- [Maintainer architecture pages](README.md): provider contracts, Terraform
  roots and identity policy, for contributors.
