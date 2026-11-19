# Metrics

What the optional metrics backend serves, how the end-to-end suite verifies it,
and what it costs. The decisions behind it are
[ADR 0024](../adr/0024-metrics-prometheus-reference-and-read-path.md); this page
records what the tree implements and what is still to be measured.

## Series

The platform API serves only these named series, from the catalog in
`packages/core/openlakeforge_core/metrics.py`. No route accepts a query. A
Prometheus series reads the recording rule `olf:<name>`; the two Dagster series
are derived from the stage's run storage and also exist with metrics off.

| Series | Scope | Labels | Source |
| --- | --- | --- | --- |
| `cpu_usage_cores` | stage, platform | `stage`, `component` | kubelet |
| `memory_working_set_bytes` | stage, platform | `stage`, `component` | kubelet |
| `container_restarts` | stage, platform | `stage`, `component` | kube-state-metrics |
| `node_cpu_usage_cores` | platform | none | kubelet |
| `node_memory_working_set_bytes` | platform | none | kubelet |
| `trino_running_queries` | stage | `stage` | Trino |
| `trino_queued_queries` | stage | `stage` | Trino |
| `trino_failed_queries` | stage | `stage` | Trino |
| `trino_heap_used_bytes` | stage | `stage` | Trino |
| `object_store_bucket_bytes` | stage, platform | `stage` | SeaweedFS |
| `object_store_disk_used_bytes` | platform | `service` | SeaweedFS |
| `object_store_disk_free_bytes` | platform | `service` | SeaweedFS |
| `pipeline_runs` | stage | `stage`, `job`, `status` | Dagster |
| `pipeline_run_duration_seconds` | stage | `stage`, `job` | Dagster |

A chart asks for `range=1h`, `6h`, `24h` or `7d`; a range longer than
`spec.observability.metrics.retention_days` is refused with a 400. A series
reads `available`, `unavailable`, `disabled` (the profile turns metrics off) or
`not_provided` (the provider has no metrics backend), never a line of zeros.

## Routes

| Route | Operation | Floor |
| --- | --- | --- |
| `GET /api/v1/stages/{stage}/metrics` | `list_stage_metrics` | `data-engineer` |
| `GET /api/v1/stages/{stage}/metrics/{series}?range=<r>` | `get_stage_metric` | `data-engineer` |
| `GET /api/v1/platform/metrics/{series}?range=<r>` | `get_platform_metric` | `data-engineer` |

The rows are in `packages/platform-api/openlakeforge_platform_api/permissions.toml`;
see [roles and permissions](roles-and-permissions.md).

## End-to-end verification

The local suite (`olf infra e2e run --env local`) checks the backend on the
running stack. Each check skips, and logs why, on a deployment it does not
apply to.

| Check | Module | Runs | What it asserts |
| --- | --- | --- | --- |
| Metrics series through the platform API | `tools/olf/olf/infra/e2e/_metrics.py` | Full, `--checks core` and `all` | The stage catalog lists every stage series; kubelet, kube-state-metrics, Trino, SeaweedFS and both Dagster series each answer a non-empty series, and so do the platform series; a role below `data-engineer` gets the stage health route's 403 |
| Metrics labels and series budget | `_metrics.py` | Full, `core` and `all` | `prometheus_tsdb_head_series` is under 10,000; every `olf:` rule has a series, and none carries a label outside `stage`, `component`, `service` and `domain` |
| Metrics backend footprint | `tools/olf/olf/infra/e2e/_metrics_drills.py` | Full, `all` only | Records the footprint below; fails on a restart or a pod at its memory limit |
| Metrics outage reads unavailable | `_metrics_drills.py` | Full, `all` only | With Prometheus scaled to zero, every Prometheus series, the catalog and the health route's `metrics` integration read `unavailable` within 15 s while a pipeline run succeeds; Prometheus is scaled back and must read `available` again |
| Metrics off | `_metrics.py` | Slim smoke (`olf infra smoke run`, and `--suite smoke`) | No Prometheus or kube-state-metrics object (Deployment, Pod, PVC, Service, ConfigMap, ServiceAccount, ClusterRole) and no NetworkPolicy that selects them; where the API answers without a token, stage health, quality and logs return 200 and the metrics series read `disabled` |

The API reads use role tokens from the e2e GitHub stub through Dex, as the
perimeter checks do, so they run on the conformance profile
(`openlakeforge.conformance.yaml`) and skip on a profile without the stub.

## Footprint

The workloads start with the requests and limits ADR 0024 proposed, set in
`infra/terraform/modules/observability/metrics/`:

| Workload | CPU request / limit | Memory request / limit |
| --- | --- | --- |
| Prometheus | 100m / 500m | 256Mi / 768Mi |
| kube-state-metrics | 10m / 100m | 32Mi / 128Mi |

The volume per `spec.storage.size` is the `metrics` row of
`release/storage-sizes.yaml`: 4Gi, 8Gi and 16Gi, with `retention.size` at 75%.

### To be measured

> [!WARNING]
> No row below holds a measured number yet. ADR 0024 asks for each size's CPU,
> memory, volume use and head series after 24 hours of running, which no CI run
> reaches. Until those are measured, the requests and limits above are the proposed
> starting values, not sized ones.

| `spec.storage.size` | Prometheus CPU | Prometheus memory | kube-state-metrics CPU | kube-state-metrics memory | Volume used | Head series |
| --- | --- | --- | --- | --- | --- | --- |
| `small` | to be measured | to be measured | to be measured | to be measured | to be measured | to be measured |
| `medium` | to be measured | to be measured | to be measured | to be measured | to be measured | to be measured |
| `large` | to be measured | to be measured | to be measured | to be measured | to be measured | to be measured |

Each Full e2e run with `--checks all` records one short measurement of the size it deployed:
the footprint check writes `.tmp/e2e-evidence/metrics-footprint-<namespace>.json`
(uploaded by `local-full-e2e.yml` as `local-e2e-evidence-<run id>`), a table in
the job summary and a `METRICS_FOOTPRINT_JSON` line in the log. That is the
input for this table, not a substitute for the 24-hour figure. The gap is
tracked in the [technical debt register](../technical-debt.md).
