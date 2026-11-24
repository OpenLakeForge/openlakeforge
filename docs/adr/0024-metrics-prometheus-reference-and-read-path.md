# ADR 0024: Optional metrics — a bounded Prometheus reference and its read path

| | |
| --- | --- |
| Status | Accepted (maintainer, 2026-11-22 and 2026-11-23): D1 to D17 bind. Open: M-f's 24-hour footprint per size (#21) |
| Built | local ✔ (slices M-a to M-e, #21; M-f's 24-hour footprint per size is open) · AWS ✘ · Azure ✘ (unsupported, D6) |

## Context

The native Portal (#333) charts resource history through the platform API
(#793), and #21 asks for an optional, resource-bounded backend to hold it. The
maintainer bound the scope in #21's 2026-11-16 comment: a minimal scrape
baseline, reads only through the platform API's allowlist, charts in the native
Portal, no kube-prometheus-stack and no Grafana or Loki dependency. #183's
2026-11-03 decision makes metrics default on for Full and off for Slim, both
overridable, and lists as non-goals unrestricted PromQL in the browser, per-run
IDs as unbounded labels, and a tracing or OpenTelemetry ingestion framework.
Nothing in the platform emits OTLP. Dagster has no Prometheus endpoint, and
Trino, SeaweedFS and the kubelet each serve one. Health is the platform API's
own signal set ([health signals](../reference/health-signals.md)); a chart is
evidence beside it.

## Decision

**D1. One Prometheus server, Prometheus 3.13 LTS, in `olf-system`, with no
collector, deployed only when metrics are enabled and supported.** A single
replica pulls every target itself and stores locally: no OpenTelemetry
Collector, no agent mode, no kube-prometheus-stack, no Grafana or Loki
dependency. The version line is the 3.13 long-term-support line, moving to the
next LTS rather than following every minor. It exists only when the resolved
`spec.observability.metrics.enabled` is true and the provider reports
`metrics.supported`; `enabled` defaults to `true` for Full and `false` for
Slim. Otherwise nothing is created: no pod, PVC, Service, ConfigMap, Role or
flow row. Its data volume is sized per `spec.storage.size` from the `metrics`
row of `release/storage-sizes.yaml`, the one file that decides every
self-hosted volume, with `retention.size` below the volume so a series
explosion shortens history instead of filling the disk. Sizes and settings:
[metrics reference](../reference/metrics.md#backend). Enforced by:
`infra/terraform/modules/observability/metrics` (`tests/metrics.tftest.hcl`),
`infra/terraform/environments/local/tests/wiring.tftest.hcl`, the `prometheus`
pin in `release/component-catalog.yaml`, `release/storage-sizes.yaml`.

**D2. The scrape baseline is minimal: the kubelet's resource metrics and
volume stats, a restricted kube-state-metrics, Trino's `/metrics`, SeaweedFS
and Prometheus itself, each with a `keep` allowlist; Dagster run metrics come
from Dagster reads, not Prometheus.** Everything outside an allowlist is
dropped at scrape time, and what is not collected is recorded in the
reference. kube-state-metrics is limited to the OLF namespaces, four resources
and an allowlist. Dagster has no Prometheus endpoint, so run counts and
durations are derived by the platform API from the runs it already reads,
served in the same envelope with `source: dagster`, and work on Slim too. That
read is bounded to 7 days and 500 runs per stage, newest first; a capped read
is never served as complete history (`complete: false`, `covered_from`).
Sources, allowlists and what is not collected:
[metrics reference](../reference/metrics.md#scrape-baseline). Enforced by:
`modules/observability/metrics/main.tf` and `kube_state_metrics.tf`;
`packages/platform-api/openlakeforge_platform_api/pipeline_series.py`.

**D3. Only `olf:` recording-rule series are served, labelled by a subset of
`stage`, `component`, `service` and `domain`; no run ID is a label; every
scrape has limits, and the deployment has a series budget.** Raw scraped
series are inputs to the rules and are never served. `stage` takes a stage
name or `platform` (nodes, `olf-system`, the ops bucket). The two Dagster
series take `job` and, on `pipeline_runs`, the run's `status` from the run
storage, both bounded. Every job sets `sample_limit`, `label_limit`,
`label_value_length_limit` and `target_limit`, and every rule group a series
`limit`, so a misbehaving target fails its scrape (`up` 0, read as
`unavailable`) instead of growing the database. The series budget is 10,000
head series on the reference deployment: an operating target the e2e asserts,
not a bound Prometheus enforces. Values:
[metrics reference](../reference/metrics.md#labels-and-limits). Enforced by:
`modules/observability/metrics/rules.tf` and `main.tf`;
`tools/olf/tests/infra/test_metrics_recording_rules.py`;
`tools/olf/olf/infra/e2e/_metrics.py`.

**D4. Only the platform API queries Prometheus, through a fixed catalog of
named series with fixed ranges and no PromQL; unavailable reads as
`unavailable`, never healthy and never zero.** A route names a series from the
catalog and a range from `1h`, `6h`, `24h`, `7d`; no route takes `start`,
`end`, `step` or a query, and the query text is server-side. A range longer
than the retention is a 400, not a short answer that looks complete. The stage
health routes' `range` is the same list (one `RANGES` in code), where it scopes
only the failed-run counts. Prometheus not answering, every target behind a
series down for the range, or no sample in the range reads `unavailable` with
a reason; a step without a sample is `null`; the profile turning metrics off
reads `disabled` and an unsupported provider `not_provided`, the
`IntegrationStatus` vocabulary. Each read is one attempt with a 5 s deadline.
Routes, ranges and envelope:
[metrics reference](../reference/metrics.md#ranges-and-responses). Enforced
by: `packages/core/openlakeforge_core/metrics.py`,
`packages/platform-api/openlakeforge_platform_api/metrics_routes.py`,
`permissions.toml`, `tools/olf/tests/common/test_metrics_catalog.py`.

**D5. Network and RBAC: Prometheus and kube-state-metrics run in `olf-system`,
Prometheus reaches the stage namespaces only for the Trino coordinators, and
its kubelet grant is `nodes/metrics`, never `nodes/proxy`.** Stage pods' CPU
and memory come from the kubelet on the node, not from the pods. Prometheus's
ClusterRole holds `nodes` `get`/`list`/`watch` and `nodes/metrics` `get` and
nothing else (`nodes/proxy` is exec-equivalent); kube-state-metrics holds a
Role per OLF namespace (`list`, `watch` on four resources) and no ClusterRole.
The one ingress row is the platform API. Every flow is a row of
`release/network-flows.yaml` (F45 to F51), rendered only when the module is
deployed; rows and grants are in [network policy and
RBAC](../architecture/identity/identity-network-policy-and-rbac.md). Enforced
by: `release/network-flows.yaml`, `modules/observability/metrics`.

**D6. The profile names a capability, never a product, and AWS and Azure are
unsupported.** No profile field selects Prometheus. AWS and Azure report
`metrics = false` and `metrics_retention = false`, so a Full profile there
reads `enabled: true, supported: false` and the Portal shows `not_provided`.
An external adapter, and any profile field to select one, is a contract change
with its own issue. Enforced by: `observability_support` in
`infra/terraform/environments/{local,aws-poc,azure-poc}/contracts.tf`.

**D7. Terraform `kubernetes_*` resources, not the community chart.** The
module writes the Deployment, PVC, ConfigMap and RBAC with pinned images, as
the log collector and alert evaluator do; the chart bundles subcharts, a reload
sidecar and a cluster-wide ClusterRole.

**D8. Prometheus's shape and hardening.** One replica with `strategy: Recreate`
and a standalone PVC; a 30 s scrape; retention by time and size in the
configuration file; query limits; no admin API, no lifecycle endpoint, no
remote write and no OTLP receiver; a non-root, read-only pod. Values: [metrics
reference](../reference/metrics.md#backend).

**D9. `retention_days` defaults to 7 for every size and preset**, #332's
value; the volumes are 4, 8 and 16 Gi with `retention.size` at 75% of each.
Revisit once the M-f 24-hour footprint is measured (#21).

**D10. `shared.observability.metrics` carries optional `query_api` and
`query_url`.** `query_api` names the protocol, never the product:
`prometheus-http-v1` is the Prometheus HTTP API's `/api/v1/query_range` and
`/api/v1/query`. Both are `null` whenever `supported` is false; local fills
them when the module is deployed. An external Prometheus-compatible backend
satisfies the contract when it answers `prometheus-http-v1` at a URL the API
reaches through a flow row, holds the catalog's `olf:` rules over the same
baseline and labels (the platform ships the reference's scrape configuration
and rule file), and needs no authentication beyond what the API sends (none in
v0.7). Enforced by: `tools/olf/olf/common/provider_contracts/_v3.py`, [provider
contracts](../architecture/provider-contracts.md#sharedobservability).

**D11. Prometheus has no authentication in v0.7.** NetworkPolicy is the
control, as for Trino ([ADR 0016](0016-trino-workload-authentication.md)): its
one ingress row is the platform API (F45). Basic auth or TLS through
`--web.config.file` is added when a second consumer (#210) needs a route.

**D12. On the local kind reference only, the kubelet job skips certificate
verification.** This is a local-only exception, to be removed by #1409. kind's
kubelets serve self-signed certificates, so an endpoint impersonating a node
address would receive Prometheus's token and could replay its node reads (node
objects and usage; no Secret, log, exec or spec). The projected token lives 600
seconds, every other provider verifies, and enabling `serverTLSBootstrap` in
the kind foundation removes the exception.

**D13. `stage` is set by scrape-time relabelling from maps Terraform renders**:
each stage namespace to its stage and `olf-system` to `platform`; for SeaweedFS
each stage's buckets to its stage and the ops bucket to `platform`, as the log
collector's routes are rendered.

**D14. Run IDs are accepted in the raw `pod` label of run pods.** A run's Job
is `dagster-run-<run_id>`, so raw kubelet and kube-state-metrics series carry
it; they are never served, are bounded by concurrent pods and the size cap, and
dropping the label would merge pods' samples. The alternative, dropping run
pods' series, loses the pipelines' share of stage usage.

**D15. The catalog lives in `openlakeforge_core`**, one module read by the API
and by tests; each entry names its scope, unit, recording rule and labels, and
the query text is parameterised only by values the API validated.

**D16. Metrics never feed `HealthState`.** A chart is evidence beside the
health signals; the health routes' `metrics` integration reports whether the
backend answers and never changes the stage's state.

**D17. The read floor is `data-engineer` on every stage**, the health routes'
floor, for stage and platform series alike, with no `role_stages` (a series
carries no log text or data) and no `services` (the alert evaluator does not
read metrics). Rows: [metrics reference](../reference/metrics.md#routes).

## Consequences

- Full gains two pods (Prometheus, kube-state-metrics) and one PVC in
  `olf-system`; Slim, or Full with `metrics.enabled: false`, gains nothing, and
  the API answers `disabled`.
- An outage of Prometheus touches no ETL path: no workload sends to it, and the
  Portal shows `unavailable` charts while health, runs, logs and quality keep
  working.
- Stage isolation is the permission matrix, not the store. Any future consumer
  that sends its own PromQL (Grafana, #210) needs its own answer to stage
  authorization and its own flow row.
- The platform API gains no Kubernetes permission; the kubelet read is
  Prometheus's.
- History is lost with the PVC; nothing backs it up. It is a convenience, not
  evidence: runs, logs and quality live in Dagster and the ops bucket.
- The requests and limits are starting values until #21's slice M-f measures
  each size's footprint over 24 hours
  ([to be measured](../reference/metrics.md#to-be-measured)).

## Boundaries

| Not decided here | Where |
| --- | --- |
| The platform API: authentication, the permission matrix, other routes | [ADR 0021](0021-platform-api.md) |
| The enforcement model and the flow-file rule | [ADR 0017](0017-local-network-policy-and-rbac.md) |
| Health states and the integration vocabulary | [Health signals](../reference/health-signals.md) |
| Alerts, which work without Prometheus | [ADR 0023](0023-alert-evaluation-and-webhook-delivery.md) |
| `spec.observability` fields and defaults | [ADR 0011](0011-deployment-profile-and-stages.md), [deployment profile](../reference/deployment-profile.md#specobservability) |
| The Portal's charts | [openlakeforge-portal#6](https://github.com/OpenLakeForge/openlakeforge-portal/issues/6) |

## Open items

The one open item is #21's slice M-f: each size's CPU, memory and volume
footprint over 24 hours, which revisits D9 and the starting requests and
limits. The ten questions of the 2026-11-17 proposal were closed on 2026-11-23:
1 (no OpenTelemetry in v0.7), 5 (run metrics from Dagster reads) and 10 (the
3.13 LTS line) by D1 and D2; 3 as "yes, restricted" (D2); 2 by D9, 4 by D14,
6 by D11, 7 by D17, 8 by D12 and 9 by D7.

## Alternatives rejected

| Option | Why not |
| --- | --- |
| kube-prometheus-stack | Maintainer: not this. The operator, its CRDs, Alertmanager, Grafana and node-exporter are far past a minimal baseline |
| OpenTelemetry Collector in front of Prometheus | A second image, pin, policy and failure mode, and nothing emits OTLP; Prometheus's own OTLP receiver covers one future source |
| Prometheus agent mode alone | Cannot answer queries; needs a second store |
| The community `prometheus` chart with subcharts off | A cluster-wide ClusterRole to replace and a sidecar image to pin, for a Deployment, a PVC and a ConfigMap |
| Following the newest Prometheus minor | A minor is superseded every six weeks; an LTS line gets a year of fixes |
| Raw PromQL through the API, filtered | #183 non-goal; a filter on a query language is a parser to keep correct |
| Browser or Portal to Prometheus directly | The Portal reads only the platform API; Prometheus has no per-stage authorization |
| A Dagster exporter or Pushgateway for run metrics | A second copy of the runs that can disagree with the runs route; user code would have to push |
| node-exporter; `/metrics/cadvisor` | Not needed for the charts; the same numbers with more series |
| The kubelet through the API server proxy (`nodes/proxy`) | Exec-equivalent grant |
| Two Prometheus replicas | Twice the footprint for a convenience; an outage reads `unavailable` and costs no ETL |
| A Prometheus per stage | Three TSDBs for a few thousand series; stage isolation is the API's rows, as for logs |

## History

- 2026-11-17 (#21): New record, as a proposal.
- 2026-11-18 (#21): D3: the maintainer keeps the `status` label on
  `pipeline_runs`. D4: the envelope carries `reason`, `complete` and
  `covered_from` on every series, and `unavailable` covers a series with no
  samples in the range; `container_restarts` and `trino_failed_queries` are
  per-minute increments summed per step.
- 2026-11-19 (#1260): D4: the stage health routes' `range` is the same list as
  the metrics routes' ranges.
- 2026-11-22: Accepted by the maintainer as built (D1 to D6); M-f's 24-hour
  footprint stays open (#21).
- 2026-11-23: Rewritten in the standard form; decisions 1 to 6 kept as D1 to
  D6, the proposed and default rows split out as D7 to D17, the build order
  dropped, and the series catalog, scrape, label, limit and sizing tables moved
  to `docs/reference/metrics.md`; no decision changed.
- 2026-11-23: D5, D7–D17 accepted by the maintainer; open items closed.
- 2026-11-23: D3: the 30-minute over-budget warning dropped (never built).
