# Introduction

OpenLakeForge is an open-source, self-hosted lakehouse for small data teams.
This page says what it assembles, who it is for, and what you take on when you
run it.

## What it is

OpenLakeForge packages ingestion, validation, table storage, a catalog, a
query engine, transformations and orchestration into one platform on
Kubernetes. Terraform and Helm come with it, and the `olf` CLI drives them, so
you do not install or wire any of those tools yourself.

| Layer | What happens | Technology |
| --- | --- | --- |
| Bronze | Source files land as CSV in object storage | dlt |
| Silver | Technical contracts validate them into Iceberg tables | Floe |
| Gold | Business marts are modelled in SQL and run in the query engine | dbt-trino on Trino |

Dagster orchestrates every run. Around that path sit the shared services:

| Capability | Local | AWS |
| --- | --- | --- |
| Table format | Apache Iceberg | Apache Iceberg |
| Catalog | Apache Polaris | AWS Glue |
| Object storage | SeaweedFS | Amazon S3 |
| Service state | PostgreSQL | Amazon RDS |
| Governance and lineage | OpenMetadata _(optional)_ | OpenMetadata _(optional)_ |
| Dashboards | Superset _(optional)_ | Superset _(optional)_ |

Infrastructure sits behind provider-neutral contracts, so the same project
runs on a laptop, AWS or Azure. See
[Architecture](../architecture/overview.md#providers).

## Who it is for

A small team that wants a working batch lakehouse without first building the
platform. Everything is code in one repository: the Deployment Profile
(`openlakeforge.yaml`) says where the platform runs, and `lakehouse_code/`
holds your sources, contracts, dbt models, Dagster jobs and dashboards.

## Is it a good fit?

| Good fit | Not yet |
| --- | --- |
| A small team that wants a batch lakehouse without building the platform first | Streaming workloads: v1 is batch-first |
| Keeping data on your own Kubernetes, storage and cloud account | Production-critical use: the project is alpha |
| Moving the same project between a laptop, AWS and Azure | Teams that want a fully managed SaaS |

## What you operate

OpenLakeForge is software you run, not a service. Once you deploy it, these
are yours:

| Responsibility | What OpenLakeForge gives you |
| --- | --- |
| The cluster and its capacity | `olf deploy` creates a local kind cluster, an EKS cluster or an AKS cluster |
| Access and sign-in | Plain HTTP on `*.olf.localhost` locally; GitHub sign-in when you add `spec.identity` ([Connect GitHub](../guides/identity/connect-github.md)) |
| Who may use which service | Canonical roles mapped to GitHub teams; membership changes happen in GitHub |
| Upgrades | Pinned component versions per release ([Compatibility matrix](../reference/compatibility-matrix.md)) |
| Diagnosis | `olf status`, `olf diagnostics collect`, `olf e2e run` |

Backups, restore drills and monitoring are on the roadmap, not in the alpha:
see the [milestones](https://github.com/OpenLakeForge/openlakeforge/milestones).

## Next

- [Quickstart](quickstart.md): a local platform with three example data
  products, in one deploy.
- [Concepts](concepts.md): the vocabulary the rest of the docs use.
