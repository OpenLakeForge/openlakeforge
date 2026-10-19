# OpenLakeForge Architecture Overview

OpenLakeForge is a cloud-agnostic, self-hostable lakehouse platform built from open-source components. The platform is Kubernetes-native and is intended to support local, on-prem, and cloud Kubernetes deployments through Terraform and Helm.

## v1 Product Shape

The v1 proof of concept proves the complete batch lakehouse path for multiple
domain-owned data products:

```text
CSV examples
  -> Bronze landing
  -> Floe validation
  -> Silver Iceberg tables through Polaris
  -> dbt-trino Gold marts
  -> Trino query
  -> Superset reports
  -> Dagster asset graph
```

The first local infrastructure target is kind. The current seed POC contains
`sales/order_revenue`, `sales/customer_health`, and
`supply_chain/inventory_reliability`. Each product has side-by-side assets under
domain capability folders: raw CSV examples, dlt Bronze loader, Floe contract
and manifest, dbt Gold models, Dagster job, Superset report bundle, and
OpenMetadata metadata nested in `lakehouse.yaml`. OpenLineage integration is
enabled for Floe and dbt-trino through OpenMetadata's native endpoint; see
ADR 0007. The OpenMetadata Dagster connector remains deferred.

## Core Platform Decisions

| Concern | Decision |
| --- | --- |
| Deployment model | Kubernetes-native from the beginning |
| Portability | Cloud-agnostic across local, on-prem, and cloud Kubernetes |
| Processing scope | Batch-first v1; streaming deferred |
| Table format | Apache Iceberg |
| Catalog | Iceberg catalog contract; local uses Apache Polaris REST |
| Local object storage | SeaweedFS |
| Query layer | Trino for analytics queries only |
| Reporting | Superset over Gold marts through Trino |
| Orchestration | Dagster asset graph |
| Lineage protocol | Native OpenLineage from Floe and dbt-trino to OpenMetadata |
| Governance target | OpenMetadata |
| IAM target | Dex with the GitHub connector, or an external OIDC issuer (ADR 0014) |
| Secret management target | Vault / External Secrets later; local uses Kubernetes Secrets |
| Ingress and TLS target | Traefik / cert-manager later; local uses port-forwarding |

## Medallion Ownership

Bronze is an immutable landing zone owned by ingestion. Silver is owned by Floe and contains technically validated Iceberg tables. Gold is owned by dbt and contains business-ready marts and analytics models.

dbt does not own a Silver staging layer in v1. Floe writes Silver Iceberg tables directly through the Polaris REST catalog. dbt consumes those Silver tables and builds Gold models.

## Project Boundary

A writable data project contains `openlakeforge.yaml` and `lakehouse_code/`.
The latter holds the provider-neutral descriptors, Bronze sources, Silver
contracts, Gold dbt projects, Superset bundles, and Dagster pipeline modules.
Terraform, Helm values, image definitions, schemas, and shared libraries stay
in the immutable OpenLakeForge distribution. Builds combine those platform
assets with the selected project's `lakehouse_code/`; they do not copy platform
files into the project.

### Deployment Profile

`openlakeforge.yaml` is the project's Deployment Profile: provider, lifecycle
stage, and preset as three separate typed concepts, never Terraform, Helm, or
Kubernetes detail (ADR 0011). `olf profile validate --project .` parses it;
`olf profile resolve --project . --json` resolves it into one effective,
machine-readable `DeploymentTopology` before anything is deployed.

The platform Terraform root consumes that topology as typed inputs and derives
the cluster's namespaces from it: one `olf-system` holding the services a
deployment runs once -- PostgreSQL, SeaweedFS, Polaris, Trino, and
OpenMetadata -- plus one `olf-<stage>` per enabled stage holding that stage's
Dagster and, where analytics is enabled, its Superset. Each stage-scoped
service instance owns its own metadata database and a stage-labelled runtime
service account. Stage data-plane isolation -- per-stage storage and catalogs
-- is #114; today every stage still resolves the shared catalog and buckets.

## Execution Model

OpenLakeForge v1 uses one custom runtime image:

```text
ghcr.io/openlakeforge/project-code:<tag>
```

The image contains:

- Dagster code
- dagster-floe connector
- dagster-dbt connector
- Floe contracts
- dbt project and models
- dlt pipelines
- domain Python code
- shared OpenLakeForge libraries

The project-code image does not install the Floe CLI. It includes generated
domain Floe manifests so the Dagster code server can load the asset graph from
the image. Floe work runs in the manifest-declared Kubernetes runner image, so
local/CD artifact upload also publishes the generated domain manifests to the
ops bucket for the runner pod.

The expected runtime flow is:

```text
Dagster webserver / daemon / code server
  -> Kubernetes run launcher
  -> isolated Dagster run pod using project-code image
  -> dlt ingestion assets
  -> Floe assets through dagster-floe and the Floe runner image
  -> dbt assets through dagster-dbt
  -> metadata catalog refresh
```

The local stack loads `ghcr.io/openlakeforge/project-code:local` into the local
kind cluster and uses it for the `openlakeforge-dagster` Dagster code location
and isolated Dagster run pods. The default code location loads
`lakehouse_code.definitions`, which aggregates every product's Dagster
definitions under `lakehouse_code/pipelines/dagster/`. Durable product jobs are
`order_revenue_pipeline`, `customer_health_pipeline`, and
`inventory_reliability_pipeline`. Each job materializes product
Bronze source assets, executes manifest-loaded Floe assets, then runs
dbt-trino Gold marts in product Gold Polaris namespaces of the `lakehouse_dev`
warehouse.

Superset is deployed as a BI consumer of those Gold marts through Trino.
Superset reports are dynamic, consumption-owned artifacts under each
`lakehouse_code/dashboards/superset/<dashboard>/` folder and are
deployed separately from Terraform bootstrap so UI edits can be exported back to
source control when they become durable report changes.
