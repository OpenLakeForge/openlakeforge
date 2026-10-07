# Architecture Decision Records

These thirteen records describe the decisions that shape OpenLakeForge **today**.
Each is binding: if the code disagrees with one, either the code is wrong or the
ADR is, and one of them gets fixed.

An ADR is rewritten in place when its decision changes, with a History footer
recording what it absorbed. The log is not an append-only archive — a superseded
decision is interesting until it stops explaining anything about the current
system, and then it lives in `git log` rather than here.

## Start here

`0001` for what the platform is made of, `0002` for how it deploys, `0004` for
how user code is laid out, and `0008` for why `olf` is the only interface.

| ADR | Decision |
| --- | --- |
| [0001](0001-platform-baseline-and-component-stack.md) | **Platform baseline and component stack** — Kubernetes-native, batch-first, Iceberg; dlt → Floe → dbt-trino → Trino, with Dagster orchestrating and OpenMetadata/Superset optional |
| [0002](0002-deployment-lifecycle.md) | **Deployment lifecycle** — three ordered phases (foundation, platform, artifacts) in two lifecycle categories (static infrastructure vs. code-derived artifacts); the static/dynamic split is the CD boundary |
| [0003](0003-provider-contracts.md) | **Provider contracts are the portability boundary** — typed Terraform contracts per capability; components consume contracts, never providers; consumers branch on `catalog_type` |
| [0004](0004-medallion-layout-and-catalog-namespaces.md) | **Medallion layout, ownership, and catalog namespaces** — `lakehouse_code/` replaces `domains/`; Bronze is source-owned, Silver domain-owned, Gold product-owned; per-layer buckets and flat namespaces |
| [0005](0005-descriptor-model.md) | **The v1alpha3 descriptor model** — `lakehouse.yaml` + `source.yaml`, one shared `openlakeforge-domain-model` implementation; v1alpha1/v1alpha2 removed |
| [0006](0006-dagster-runtime-and-code-locations.md) | **Dagster runtime and code locations** — one `project-code` image, one merged code location by default, Kubernetes run launcher, Floe in its own manifest-driven runner |
| [0007](0007-governance-and-lineage.md) | **Governance and lineage** — OpenMetadata with native OpenLineage emitted by the engine that performed the write; the proxy and custom REST push stay rejected |
| [0008](0008-olf-owns-orchestration-and-toolchain.md) | **`olf` owns orchestration and its managed toolchain** — no tracked shell, `olf` provisions its own Terraform/Helm/kubectl/kind, cloud auth through SDKs |
| [0009](0009-distribution.md) | **Distribution** — a PyPI package carrying a verified platform payload; installed project root is the current directory; `olf init` |
| [0010](0010-cloud-provider-implementations.md) | **Cloud provider implementations** — AWS replaces contract implementations (S3, RDS, Glue, Pod Identity), Azure relocates hosting (AKS/ACR); both POC maturity |
| [0011](0011-deployment-profile-and-stages.md) | **Deployment Profile v1** — provider, stage, and preset are separate typed concepts; `openlakeforge.yaml` resolves to one effective `DeploymentTopology` before any Terraform, Helm, or Kubernetes object exists |
| [0012](0012-project-revisions-and-promotion.md) | **Project revisions** — a build-once, content-addressed `ProjectRevision` covering descriptors, Floe contracts, dbt, Dagster, reports, and the project-code image digest; excludes deployment intent and every stage-rendered artifact |
| [0013](0013-service-access-and-ingress.md) | **Service access** — one ingress everywhere, configured by `access.base_domain` + `access.issuer`; stage-derived HTTPS routes in `shared.access`; internal endpoints cannot be user-facing; port-forward only as a dev fallback |
| [0014](0014-identity-and-authorization.md) | **Identity and authorization** — four seams (role model, issuer, perimeter, admin), each with its own adapter; canonical roles are the only vocabulary and provider mappings are keyed by them; the fixed role model is built, the rest is decided direction; OpenLakeForge roles are never cloud IAM |

## Related

- [`../technical-debt.md`](../technical-debt.md) — known weaknesses, mitigations,
  and the fix path for each
- [`../architecture/README.md`](../architecture/README.md) — how the platform
  works, as opposed to why it was decided
