# OpenLakeForge Documentation

Welcome to the OpenLakeForge documentation.

OpenLakeForge is an open-source, self-hosted lakehouse platform designed for small data teams. This documentation covers how to **install the platform, deploy it, build data products, operate it, and understand its architecture**.

If you are discovering OpenLakeForge for the first time, start with the [root README](../README.md).

## Choose your path

| I want to...                          | Start here                                                       |
| ------------------------------------- | ---------------------------------------------------------------- |
| 🚀 Try OpenLakeForge locally          | [Local installation](setup/local.md)                             |
| ☁️ Deploy to AWS                      | [AWS deployment](setup/cloud-poc-setup.md#aws-eks)               |
| ☁️ Deploy to Azure                    | [Azure deployment](setup/cloud-poc-setup.md#azure-aks)           |
| 🧱 Build my first data product        | [Your first data product](getting-started/first-data-product.md) |
| 📄 Understand `lakehouse.yaml`        | [Domain descriptor reference](reference/domain-descriptor.md)    |
| 🔁 Promote a revision through stages in CI | [Reference CI/CD](reference/ci-cd.md)                      |
| 🛠️ Use the `olf` CLI                 | [`olf` CLI reference](../tools/olf/README.md)                    |
| 🏗️ Understand the architecture       | [Architecture documentation](architecture/README.md)             |
| 🔌 Understand cloud portability       | [Provider contracts](architecture/provider-contracts.md)         |
| 🧠 Understand architectural decisions | [Architecture Decision Records](adr/README.md)                   |
| 📦 Check supported versions           | [Compatibility matrix](release/compatibility-matrix.md)          |
| 🗺️ See where the project is going    | [Industrialization roadmap](industrialization-roadmap.md)        |
| 🤝 Contribute to OpenLakeForge        | [Contributor guide](../AGENTS.md)                                |

---

## Getting started

### Local installation

The local environment is the quickest way to evaluate OpenLakeForge.

It runs on a local Kubernetes cluster using `kind` and requires a working Docker engine together with the OpenLakeForge deployment tooling.

The local platform is available in two presets:

* **Slim** — the core ingestion-to-Gold lakehouse stack.
* **Full** — Slim plus OpenMetadata governance and Superset dashboards.

`--provider local --profile slim|full` is the deprecated single-DEV-stage
shorthand for the typed Deployment Profile (`openlakeforge.yaml`), which
`olf profile validate`/`resolve` parse and resolve — see ADR 0011.

➡️ [Install OpenLakeForge locally](setup/local.md)

### AWS

OpenLakeForge can deploy the same platform architecture to AWS using services including:

* Amazon EKS
* Amazon ECR
* Amazon S3
* Amazon RDS for PostgreSQL
* AWS Glue Data Catalog

The AWS environment is currently named `aws-poc` because it has been validated in sandbox environments. The POC designation describes its validation maturity, not a reduced platform implementation.

➡️ [Deploy OpenLakeForge to AWS](setup/cloud-poc-setup.md#aws-eks)

### Azure

The Azure deployment runs OpenLakeForge on AKS and ACR while keeping the same OpenLakeForge platform interfaces used by the other environments.

As with AWS, the `azure-poc` name reflects the environment in which the deployment is currently validated.

➡️ [Deploy OpenLakeForge to Azure](setup/cloud-poc-setup.md#azure-aks)

---

## Building data products

OpenLakeForge organizes workloads around **domains and data products**.

A data product typically contains the assets required to move data through the complete lakehouse path:

```text
Source
  ↓
dlt ingestion
  ↓
Bronze
  ↓
Floe validation
  ↓
Silver Iceberg
  ↓
dbt-trino
  ↓
Gold Iceberg
  ↓
Trino
```

Dagster orchestrates the resulting asset graph.

### Start with a tutorial

The recommended way to understand the data-product model is to build one end to end.

➡️ [Build your first data product](getting-started/first-data-product.md)

### Domain descriptors

Each project has a `lakehouse.yaml` descriptor defining its domains, data
products, and their Bronze, Silver, and Gold assets, plus a `source.yaml`
descriptor per Bronze source.

Descriptors are provider-neutral: business metadata describes the logical data product while OpenLakeForge resolves infrastructure-specific catalog, storage, and namespace details through provider contracts.

Resources:

* [Lakehouse descriptor JSON schema](schema/lakehouse.schema.json)
* [Source descriptor JSON schema](schema/source.schema.json)
* [Domain descriptor reference](reference/domain-descriptor.md)
* [Provider contracts](architecture/provider-contracts.md)

---

## OpenLakeForge tooling

### `olf` CLI

OpenLakeForge contains a Python-based deployment tool named `olf`.

It provides shared cross-environment functionality used by local, AWS, and Azure deployments, including:

* provider-contract resolution
* catalog namespace reconciliation
* Floe profile generation
* artifact and manifest deployment
* OpenMetadata metadata deployment
* Superset report deployment
* Kubernetes project-code image updates
* release validation
* end-to-end environment testing
* a managed Terraform/Helm/kubectl/kind toolchain, so those tools do not
  need to be installed on the host

`olf` is the primary supported OpenLakeForge interface for deployment, checks, and release workflows; a checkout's `Makefile` provides deprecated one-line delegates to the same `olf` commands and is being removed (#222).

➡️ [`olf` CLI documentation](../tools/olf/README.md)

### Domain model Python package

OpenLakeForge also contains a provider-neutral Python domain model under:

```text
packages/domain-model/
```

It provides the canonical implementation for loading and validating domain descriptors and constructing the OpenLakeForge domain inventory.

This package is shared by OpenLakeForge deployment tooling and runtime components.

---

## Architecture

If you want to understand **how OpenLakeForge works internally**, start here:

➡️ [Architecture documentation](architecture/README.md)

The architecture documentation covers:

* Kubernetes runtime topology
* Dagster execution model
* Bronze / Silver / Gold ownership
* Apache Iceberg and catalog integration
* Floe validation
* dbt-trino transformations
* OpenMetadata lineage
* Superset reporting
* Terraform deployment boundaries
* cloud-provider portability
* project revisions and stage promotion (see
  [Project revisions](architecture/project-revisions.md))

### Architecture diagrams

For a visual representation of the platform:

➡️ [Technical architecture diagrams](architecture/diagrams/README.md)

The diagrams cover the runtime topology, ephemeral jobs, medallion data path, provider contracts, and Kubernetes workload model.

### Provider contracts

OpenLakeForge isolates platform capabilities from infrastructure implementations through provider-neutral contracts.

This is what allows the same platform to use, for example:

```text
Local                     AWS
────────────────────────────────────────
kind                      EKS
SeaweedFS                 S3
PostgreSQL                RDS PostgreSQL
Apache Polaris            AWS Glue
```

without changing the logical data-product model.

➡️ [Provider contracts](architecture/provider-contracts.md)

---

## Architecture Decision Records

OpenLakeForge uses Architecture Decision Records (ADRs) for decisions that materially affect the platform architecture.

They document not only the current architecture, but **why a decision was made and which previous decisions it supersedes**.

➡️ [Browse Architecture Decision Records](adr/README.md)

ADRs are useful if you want to understand subjects such as:

* provider-neutral cloud architecture
* Iceberg catalog abstractions
* deployment phases
* Dagster code locations
* artifact deployment
* OpenLineage
* domain descriptor versions
* the canonical domain model

---

## Releases and compatibility

OpenLakeForge releases pin and track the infrastructure and software components used by the platform.

### Compatibility matrix

The compatibility matrix records the versions of:

* Terraform
* Terraform providers
* Helm charts
* container images
* Kubernetes environments
* cloud services

➡️ [Compatibility matrix](release/compatibility-matrix.md)

### Release process

For maintainers and contributors working on OpenLakeForge releases:

➡️ [Release process](release/releasing.md)

Release artifacts include versioned component metadata, checksums, SBOMs and build provenance.

### Changelog

➡️ [OpenLakeForge changelog](../CHANGELOG.md)

---

## Project status and roadmap

OpenLakeForge is currently in **alpha** and under active development.

The project is moving from a working multi-environment reference platform toward a distribution optimized for small-team adoption.

Current priorities include:

* reducing the default runtime footprint
* improving installation from release artifacts
* simplifying data-product onboarding
* improving deployment validation
* strengthening recovery and operational workflows
* progressively defining stable support boundaries

➡️ [Industrialization roadmap](industrialization-roadmap.md)

---

## Contributors

If you want to change OpenLakeForge itself rather than consume it as a platform, see the contributor documentation.

➡️ [Contributor and repository guide](../AGENTS.md)

It covers the repository structure, architecture rules, development conventions, validation commands, and the checks expected before contributing changes.

---

## Documentation structure

```text
docs/
├── README.md
│
├── getting-started/
│   └── first-data-product.md
│
├── setup/
│   ├── local.md
│   └── cloud-poc-setup.md
│
├── reference/
│   └── domain-descriptor.md
│
├── architecture/
│   ├── README.md
│   ├── overview.md
│   ├── provider-contracts.md
│   └── diagrams/
│
├── adr/
├── release/
├── schema/
└── testing/
```

The documentation is intentionally separated by audience:

* **Getting started** explains how to use OpenLakeForge.
* **Setup** explains how to deploy it.
* **Reference** documents configuration and interfaces.
* **Architecture** explains how the platform works.
* **ADRs** explain why architectural decisions were made.
* **Release and testing documentation** primarily targets maintainers and contributors.
