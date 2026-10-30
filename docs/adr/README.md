# Architecture Decision Records

These twenty-one records describe the decisions that shape OpenLakeForge **today**.
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
| [0005](0005-descriptor-model.md) | **The descriptor model** — `governance.yml` (domains and products) + `source.yaml`, one shared `openlakeforge-domain-model` implementation; tables belong to a domain or product by schema |
| [0006](0006-dagster-runtime-and-code-locations.md) | **Dagster runtime and code locations** — one `project-code` image, one merged code location by default, Kubernetes run launcher, Floe as one Kubernetes Job per domain on the same image |
| [0007](0007-governance-and-lineage.md) | **Governance and lineage** — OpenMetadata with native OpenLineage emitted by the engine that performed the write; the proxy and custom REST push stay rejected |
| [0008](0008-olf-owns-orchestration-and-toolchain.md) | **`olf` owns orchestration and its managed toolchain** — no tracked shell, `olf` provisions its own Terraform/Helm/kubectl/kind, cloud auth through SDKs |
| [0009](0009-distribution.md) | **Distribution** — a PyPI package carrying a verified platform payload; installed project root is the current directory; `olf init` |
| [0010](0010-cloud-provider-implementations.md) | **Cloud provider implementations** — AWS replaces contract implementations (S3, RDS, Glue, Pod Identity), Azure relocates hosting (AKS/ACR); both POC maturity |
| [0011](0011-deployment-profile-and-stages.md) | **Deployment Profile v1** — provider, stage, and preset are separate typed concepts; `openlakeforge.yaml` resolves to one effective `DeploymentTopology` before any Terraform, Helm, or Kubernetes object exists |
| [0012](0012-project-revisions-and-promotion.md) | **Project revisions** — a build-once, content-addressed `ProjectRevision` covering descriptors, Floe contracts, dbt, Dagster, reports, and the project-code image digest; excludes deployment intent and every stage-rendered artifact |
| [0013](0013-service-access-and-ingress.md) | **Service access** — one ingress everywhere, configured by `access.base_domain` + `access.issuer`; stage-derived HTTPS routes in `shared.access`; internal endpoints cannot be user-facing; port-forward only as a dev fallback |
| [0014](0014-identity-and-authorization.md) | **Identity and authorization** — three seams (role model, issuer, perimeter), each with its own adapter; canonical roles are the only vocabulary and provider mappings are keyed by them; GitHub teams through Dex are the deployed issuer, with `external` for any other OIDC issuer; user administration is GitHub's; OpenLakeForge roles are never cloud IAM |
| [0015](0015-polaris-identity-provider-evaluation.md) | **Polaris identity-provider evaluation** — Polaris 1.4.0 can validate issuer tokens but still owns principals and grants; deferred, with the catalog-side findings for the data-plane assessment (#179) |
| [0016](0016-trino-workload-authentication.md) | **Trino workload authentication** — password-file authentication over HTTPS for Superset, dbt, OpenMetadata and `olf e2e`; `principals` rules bind each verified credential to one stage user; proven in a standalone container, callers not yet migrated |
| [0017](0017-local-network-policy-and-rbac.md) | **Local NetworkPolicy and RBAC** — default-deny ingress and egress for `olf-system` and stage namespaces with a derived flow table, an enforcing-dataplane gate and canary, the RBAC inventory with its exceptions, and the conformance plan; design only, nothing built |
| [0018](0018-github-is-the-forge.md) | **GitHub is the forge** — source, CI, release and project images on GitHub today; client CD in GitHub Actions and people and roles from GitHub organisation teams through Dex are decided; `olf users` removed |
| [0019](0019-data-plane-authorization-deferral.md) | **Data-plane authorization — Dex identity, Polaris grants** — decided, not built: today service-level only; `olf infra query` moves to Dex sign-in, with the canonical role reaching Polaris, whose per-stage catalog grants decide read and write by layer (`analyst` Gold, `viewer` UAT and PROD Gold, `data-engineer` DEV write, UAT and PROD read); engine-agnostic: no engine carries roles, and Polaris vends the storage credentials a person's query reads with, so DuckDB can follow Trino; AWS and Glue out of scope; Superset stays on the shared principal (V/T marked, #367) |
| [0020](0020-infrastructure-common-and-platform-cli-surfaces.md) | **Infrastructure, common and platform CLI surfaces** — a command's surface follows the credential it needs: `olf infra` for cluster and cloud permissions, top-level for no permissions, and a Dex-session platform surface still to come; old paths stay as hidden aliases; non-interactive platform authentication is short-lived, scoped and never a human token |
| [0021](0021-platform-api.md) | **The platform API** — decided, build step 1 begun (every service client and the Superset report operations are in `packages/lakehouse-services`): one Python backend in this repository serves the portal, `olf`'s platform commands and agents; data-plane calls carry the person's token to Polaris grants, control-plane calls (runs, domains and products, reports, logs) use the API's own narrowest service credentials, so the API validates tokens and enforces a tested role matrix; service operations move to a shared library both `olf infra` and the API call, and the artifacts phase keeps calling it directly |

## Related

- [`../technical-debt.md`](../technical-debt.md) — known weaknesses, mitigations,
  and the fix path for each
- [`../architecture/README.md`](../architecture/README.md) — how the platform
  works, as opposed to why it was decided
