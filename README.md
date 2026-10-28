# OpenLakeForge

**An open-source lakehouse platform for small data teams — self-hosted, modular, and designed to run anywhere.**

[![Release](https://img.shields.io/github/v/release/OpenLakeForge/openlakeforge?include_prereleases)](https://github.com/OpenLakeForge/openlakeforge/releases)  
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](/LICENSE)  
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](/docs/release/releasing.md#lifecycle-stages)

Building a modern data platform usually means assembling and operating
ingestion, orchestration, storage, catalog, governance and analytics tools.
**OpenLakeForge packages those pieces into one opinionated, open-source
lakehouse**, so small data teams can focus on data products instead of
becoming platform engineers. Run only the core data path, add governance and
dashboards when you need them, and keep the same architecture locally, on AWS
or on Azure.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-local-dark.svg">
  <img alt="OpenLakeForge architecture: Dagster orchestrates dlt, Floe and dbt-trino over Bronze, Silver and Gold Iceberg tables in object storage, queried by Trino and Superset and governed by OpenMetadata" src="docs/assets/architecture-local-light.svg">
</picture>

> How the pieces fit together: [Architecture](/docs/architecture/overview.md).

## Why OpenLakeForge?

- 🪶 **Built for small data teams** — less operational work to run a modern lakehouse.
- 🧩 **Modular** — use the core data platform alone, or add governance and analytics.
- 🔓 **Open and self-hosted** — your infrastructure, your storage, your data.
- ☁️ **Deploy anywhere** — local Kubernetes, AWS and Azure share the same platform contracts.
- 🧊 **Open table format** — Apache Iceberg keeps storage independent from the engines that use it.
- 🏗️ **Infrastructure included** — Kubernetes, Terraform and Helm come with the platform.

## How it works

Data moves through three medallion layers, each owned by one tool:

|Layer|What happens|Technology|
|---|---|---|
|**Bronze**|Source files land as CSV in object storage|dlt|
|**Silver**|Technical contracts validate them into Iceberg tables|Floe|
|**Gold**|Business marts are modelled in SQL and run in the query engine|dbt-trino on Trino|

Dagster orchestrates every run. Around that path sit the shared services:

|Capability|Technology|
|---|---|
|Table format|Apache Iceberg|
|Data catalog|Apache Polaris / AWS Glue|
|Object storage|S3-compatible storage (SeaweedFS) / AWS S3|
|Service state|PostgreSQL / Amazon RDS|
|Governance and lineage|OpenMetadata _(optional)_|
|Dashboards|Superset _(optional)_|

Infrastructure sits behind provider-neutral contracts, so local, AWS and Azure
expose the same platform capabilities with different implementations.
→ [How the providers differ](/docs/architecture/overview.md#providers)

### Is it a good fit?

|Good fit|Not yet|
|---|---|
|A small team that wants a working batch lakehouse without building the platform first|Streaming workloads: v1 is batch-first|
|Keeping data on your own Kubernetes, storage and cloud account|Production-critical use: the project is alpha|
|Moving the same project between a laptop, AWS and Azure|Teams that want a fully managed SaaS|

## Quickstart

Deploy a local, single-stage Slim platform on Kubernetes in Docker. You need
**Docker** and **[uv](https://docs.astral.sh/uv/)**; `olf` provisions its own
pinned Terraform, Helm, kubectl and kind.

```bash
uv tool install "openlakeforge==0.5.0a1" --python 3.12

mkdir my-lakehouse && cd my-lakehouse
olf init                      # writes openlakeforge.yaml and lakehouse_code/
olf deploy --provider local   # builds the kind cluster and the whole platform
```

Then open the portal at `http://olf.localhost`, run an example data product in
Dagster and check it end to end: the
[Quickstart](docs/get-started/quickstart.md) walks through each step. New to
the vocabulary? Read the [Introduction](docs/get-started/introduction.md) and
[Concepts](docs/get-started/concepts.md).

### Your project

`olf init` creates a writable project. The platform itself (Terraform, Helm,
images) stays in the installed distribution, so your repository holds only
what you own:

```text
my-lakehouse/
├── openlakeforge.yaml      # Deployment Profile: provider, stages, preset
└── lakehouse_code/
    ├── lakehouse.yaml      # domains and data products
    ├── bronze/             # sources and their dlt loaders
    ├── silver/             # Floe project: one file per entity, grouped by domain
    ├── gold/               # dbt projects per product
    ├── pipelines/dagster/  # one Dagster job per product
    └── dashboards/superset/
```

Add your own with `olf source new`, `olf domain new` and `olf product new`; the
[Tutorial](/docs/tutorial/01-tour-the-platform.md) builds one step by step.
The default **Slim** preset runs the ingestion-to-Gold path; `preset: full`
adds OpenMetadata and Superset.

## Stages: from a commit to PROD

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/stage-promotion-dark.svg">
  <img alt="Stage promotion: a merged pull request builds one image and one project revision, which is activated in the DEV namespace, tested, approved, and then activated unchanged in PROD" src="docs/assets/stage-promotion-light.svg">
</picture>

A profile can enable `dev`, an optional `uat`, and `prod`, each with its own
namespace, Dagster, buckets and catalog in one cluster. A change is built
**once** into an immutable project revision, activated in DEV, validated, then
activated unchanged in PROD; PROD never rebuilds. Two GitHub Actions automate
it: [CI/CD and environment promotion](/docs/guides/promote-dev-to-prod.md).

## Deploy to the cloud

The same project runs in your own cloud account:

- ☁️ **AWS** — EKS, ECR, S3, RDS PostgreSQL and Glue:
  [Deploy to AWS](/docs/guides/cloud/deploy-to-aws.md)
- ☁️ **Azure** _(preview)_ — AKS and ACR with the in-cluster platform services:
  [Deploy to Azure](/docs/guides/cloud/deploy-to-azure.md)

## Documentation

- 📚 [Documentation home](/docs/README.md): Introduction → Quickstart → Tutorial
- 🧭 [Guides](/docs/guides/local-installation.md) ·
  📖 [CLI reference](/docs/reference/cli.md) ·
  🏗️ [Architecture](/docs/architecture/overview.md)
- 📦 [Compatibility matrix](/docs/reference/compatibility-matrix.md) ·
  📝 [Changelog](/CHANGELOG.md) ·
  🗺️ [Roadmap](https://github.com/OpenLakeForge/openlakeforge/milestones)

## Project status

OpenLakeForge is **alpha** and under active development. Local, AWS and Azure
exercise the same platform; their validation and support maturity differ.
Follow the [milestones](https://github.com/OpenLakeForge/openlakeforge/milestones)
for the current direction.

## Contributing

Contributions, experiments and feedback are welcome. Start with [CONTRIBUTING.md](/CONTRIBUTING.md); [AGENTS.md](/AGENTS.md) contains the repository map, architectural rules, development workflow and validation gates.

## License

OpenLakeForge is released under the [Apache License 2.0](/LICENSE).
