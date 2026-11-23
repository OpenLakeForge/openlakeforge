# Architecture Decision Records

These twenty-four records describe the decisions that shape OpenLakeForge **today**.
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
| [0001](0001-platform-baseline-and-component-stack.md) | **Platform baseline and component stack** — Kubernetes-native, batch-first, Iceberg; dlt → Floe → dbt-trino → Trino under Dagster; Slim and Full |
| [0002](0002-deployment-lifecycle.md) | **Deployment lifecycle** — foundation, platform, artifacts in order; the static/dynamic split is the CD boundary |
| [0003](0003-provider-contracts.md) | **Provider contracts are the portability boundary** — Terraform exports typed v3 contracts per stage; `olf` parses them fail-closed; consumers branch on `catalog_type` |
| [0004](0004-medallion-layout-and-catalog-namespaces.md) | **Medallion layout, ownership, and catalog namespaces** — `lakehouse_code/` replaces `domains/`; Bronze is source-owned, Silver domain-owned, Gold product-owned; per-layer buckets and flat namespaces |
| [0005](0005-descriptor-model.md) | **The descriptor model** — `governance.yml`, `source.yaml` and optional `alerts.yml`, implemented once in `openlakeforge-core`; tables belong by schema |
| [0006](0006-dagster-runtime-and-code-locations.md) | **Dagster runtime and code locations** — one `project-code` image, one merged code location by default, Kubernetes run launcher, Floe as one Kubernetes Job per domain on the same image |
| [0007](0007-governance-and-lineage.md) | **Governance and lineage** — optional OpenMetadata; lineage only from the engine that wrote; quality results published after the run |
| [0008](0008-olf-owns-orchestration-and-toolchain.md) | **`olf` owns orchestration and its managed toolchain** — no tracked shell, a pinned private toolchain, cloud sign-in through SDKs |
| [0009](0009-distribution.md) | **Distribution** — a PyPI package carrying a verified platform payload; the project root is the current directory; `olf init` |
| [0010](0010-cloud-provider-implementations.md) | **Cloud provider implementations** — AWS swaps in EKS, S3, RDS, ECR, Pod Identity and one prefixed Glue catalog; Azure relocates hosting to AKS/ACR; both proof of concept |
| [0011](0011-deployment-profile-and-stages.md) | **Deployment Profile v1beta1** — provider, stage, preset and sizing are distinct types; `openlakeforge.yaml` resolves to one `DeploymentTopology` before anything is applied |
| [0012](0012-project-revisions-and-promotion.md) | **Project revisions** — a build-once, content-addressed `ProjectRevision` covering descriptors, Floe contracts, dbt, Dagster, reports, and the project-code image digest; excludes deployment intent and every stage-rendered artifact |
| [0013](0013-service-access-and-ingress.md) | **Service access** — one ingress switched on by `spec.access`; `<service>-<stage>` routes in the contract; HTTP on `*.localhost` by default |
| [0014](0014-identity-and-authorization.md) | **Identity and authorization** — canonical roles, the `identity.oidc` issuer (GitHub through Dex, or external) and the sign-in perimeter; members are administered in GitHub |
| [0015](0015-polaris-identity-provider-evaluation.md) | **Polaris identity-provider evaluation** — Polaris 1.4.0 can validate issuer tokens but still owns principals and grants; deferred, with the catalog-side findings for the data-plane assessment (#179) |
| [0016](0016-trino-workload-authentication.md) | **Trino workload authentication — none** — every caller sends its own token and Polaris decides; fixed-user Web UI behind the perimeter; AWS keeps Glue rules until #681 |
| [0017](0017-local-network-policy-and-rbac.md) | **Local NetworkPolicy and RBAC** — default deny for stage namespaces and the listed `olf-system` workloads, every flow a row of one file, an enforcing-dataplane gate, kindnet as the CNI, RBAC exceptions named and pinned |
| [0018](0018-github-is-the-forge.md) | **GitHub is the forge** — source, CI, release, GHCR, and people and roles from GitHub teams through Dex; client CD in GitHub Actions not built |
| [0019](0019-data-plane-authorization-deferral.md) | **Data-plane authorization — Dex identity, Polaris grants** — built for local (#674): `olf query` runs a person's SQL with their Dex sign-in, and their canonical role reaches Polaris, whose per-stage catalog grants decide read and write by layer (`analyst` Gold, `viewer` UAT and PROD Gold, `data-engineer` DEV write, UAT and PROD read); engine-agnostic: no engine carries roles, and Polaris vends the storage credentials a person's query reads with, so DuckDB can follow Trino; AWS to follow on Polaris instead of Glue (#681); Superset queries per user (V/T marked, #367); section 9 (#960, built for local) extends Polaris to every caller: one Trino per stage, per-stage processing principals, a keyless engine, Superset per user, OpenMetadata governing its own users |
| [0020](0020-infrastructure-common-and-platform-cli-surfaces.md) | **Infrastructure, common and platform CLI surfaces** — the credential a command needs decides its surface; old paths stay as warning aliases |
| [0021](0021-platform-api.md) | **The platform API** — one Python backend for the portal, `olf` and agents; the person's token where the service authorizes, a tested role matrix and audit elsewhere |
| [0022](0022-pod-log-collection-and-archive-format.md) | **Pod log collection and the log archive format** — built for local (#1120 to #1189, #22): a Fluent Bit DaemonSet replaces polling as the pod log path where it runs (AWS and Azure keep the CronJob) and writes gzipped NDJSON (archive format v2) to the ops bucket with no Loki; Alloy does not qualify; routing is by the namespace in the log file name, never pod metadata, and collection makes no per-stage confidentiality claim; the v1 archive stays readable through the same `LogArchive` reader |
| [0023](0023-alert-evaluation-and-webhook-delivery.md) | **Alert evaluation, alert policy, incident state and webhook delivery** — evaluator built on local (#1168 to #1173): a `*/5` CronJob in `olf-system` running the platform API image evaluates the canonical health signals, keeps incident state in the ops bucket under `alerts/`, and notifies once on fire and once on resolve through one HTTPS webhook whose Secret-held URL is the credential; no acknowledgement, no Prometheus, and a full-cluster outage cannot alert. Alert policy built (AP-a to AP-d): a project's `lakehouse_code/alerts.yml` (`AlertPolicy`) declares per product and stage which conditions alert, with which grace and freshness thresholds, and through which named routes to the profile's receivers; silent unless declared; the most specific rule wins; platform conditions go to `platform_receivers`; the evaluator reads the policy from the stage's active revision and keeps the last good one |
| [0024](0024-metrics-prometheus-reference-and-read-path.md) | **Optional metrics: a bounded Prometheus reference and its read path** — accepted as built (#21; the M-f acceptance and 24-hour footprint stay open): one Prometheus 3.13 LTS server in `olf-system` with no OpenTelemetry Collector, deployed only when `observability.metrics` is enabled and supported; a minimal baseline (kubelet resource metrics with `nodes/metrics` only, restricted kube-state-metrics, Trino's `/metrics`, SeaweedFS) with per-scrape limits, a series budget and a per-size volume; Dagster run counts and durations come from Dagster reads in the platform API, not Prometheus; only the platform API queries it, through a fixed catalog of named recording-rule series labelled by `stage`, `component`, `service` and `domain`, with fixed ranges and no PromQL; unavailable reads as `unavailable`, never healthy or zero; AWS and Azure stay unsupported |

## How to write an ADR

Start from [`TEMPLATE.md`](TEMPLATE.md). A record states what binds, not how
it was built: each decision has a stable ID (**D1.**, **D2.**, …) that is
never renumbered, because code and other documents cite it, and a decision
that stops binding stays as "**D3. Withdrawn (date):** reason". Each decision
is a one-sentence rule, a short paragraph, and the file or check that
enforces it. Evidence tags, build orders, per-PR narrative and test
inventories stay out: issues, `git log` and `CHANGELOG.md` hold them, and
History gets one line per change to a decision. Reference tables live in
`docs/reference/` or `docs/architecture/`, and the record links to them.

**Approval.** When an ADR is written or rewritten, the pull request carries a
review thread on its `## Decision` heading. The maintainer approves that
Decision text by resolving the thread; since every review thread must be
resolved before merge, the pull request cannot merge until each one is.

## Related

- [`../technical-debt.md`](../technical-debt.md) — known weaknesses, mitigations,
  and the fix path for each
- [`../architecture/README.md`](../architecture/README.md) — how the platform
  works, as opposed to why it was decided
