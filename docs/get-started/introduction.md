# Introduction

OpenLakeForge is an open-source lakehouse you run entirely in your own
infrastructure, small enough for one data team to operate. This page says what
it assembles, who it is for, and what you take on when you run it.

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

Data teams that have to own their data platform and want a complete one
without building it first:

- **Teams whose data must stay in their own infrastructure.** Data
  sovereignty, data residency or sector rules (public sector, health,
  finance) rule out a SaaS warehouse; every component runs in your cluster,
  storage and cloud account.
- **Data teams that already run Kubernetes** and want an open,
  predictable-cost alternative to a proprietary warehouse.
- **Consultancies and integrators** that deliver the same platform to several
  clients: one pinned, tested release they can reproduce per client.

In each case the team running it is small: the platform comes assembled, so
it does not need a dedicated platform engineer to start.

Everything is code in one repository: the Deployment Profile
(`openlakeforge.yaml`) says where the platform runs, and `lakehouse_code/`
holds your sources, contracts, dbt models, Dagster jobs and dashboards.

## Is it a good fit?

| Good fit | Not yet |
| --- | --- |
| Data that must stay on your own Kubernetes, storage and cloud account | Streaming workloads: v1 is batch-first |
| A data team on Kubernetes that wants an open alternative to a proprietary warehouse | Production-critical use: the project is alpha |
| An integrator deploying the same platform for several clients | Teams that want a fully managed SaaS |
| A small team that wants a batch lakehouse without building the platform first | A small team with no Kubernetes and no residency constraint: a managed warehouse is simpler |

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
