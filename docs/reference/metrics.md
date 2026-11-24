# Metrics

What the optional metrics backend serves, how the end-to-end suite verifies it,
and what it costs. The decisions behind it are
[ADR 0024](../adr/0024-metrics-prometheus-reference-and-read-path.md); this page
records what the tree implements and what it measured.

## Backend

One Prometheus server in `olf-system`, deployed by
`infra/terraform/modules/observability/metrics` only when the resolved
`metrics.enabled` is true and the provider reports `metrics.supported`. Image
`prom/prometheus` 3.13 LTS, pinned by digest in `release/component-catalog.yaml`
(`prometheus`); kube-state-metrics runs beside it (`kube_state_metrics`).

| Setting | Value | Why |
| --- | --- | --- |
| Workload | Deployment, one replica, `strategy: Recreate`, and a separate PersistentVolumeClaim | A standalone claim can be expanded in place; a StatefulSet's claim template cannot change ([deployment profile](deployment-profile.md#changing-the-storage-size)). Two replicas would write one TSDB twice |
| Scrape interval | 30 s, timeout 10 s | The finest chart step is 60 s |
| Retention | `storage.tsdb.retention.time` (`<retention_days>d`) and `storage.tsdb.retention.size` (the size row under [Footprint](#footprint)), in the configuration file; whichever is reached first deletes the oldest block | The `--storage.tsdb.retention.*` flags are deprecated at 3.13; without either setting the default is 15 days |
| Query limits | `--query.timeout=10s`, `--query.max-samples=5000000`, `--query.max-concurrency=4` | Stop a bad template from exhausting the pod |
| Admin | No `--web.enable-admin-api`, no `--web.enable-lifecycle`, no remote write, no OTLP receiver | Nothing deletes series or reloads over HTTP; a configuration change is a rollout |
| Pod | uid 65534, read-only root filesystem except the data volume, no capability, `RuntimeDefault` seccomp | As the platform API |
| Service account token | Projected, `expirationSeconds: 600` | The kubelet job on kind skips certificate verification ([ADR 0024](../adr/0024-metrics-prometheus-reference-and-read-path.md), D12) |

## Scrape baseline

Each source has a `keep` allowlist of metric names applied at scrape time, so
everything else is dropped before it is stored.

| Source | How it is scraped | Kept |
| --- | --- | --- |
| kubelet resource metrics | HTTPS to each node's port 10250, path `/metrics/resource`, discovered with `kubernetes_sd_configs` role `node`, the ServiceAccount token as bearer. Authorized as `nodes/metrics`; `nodes/proxy`, which the kubelet treats as exec, is never granted | `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `node_cpu_usage_seconds_total`, `node_memory_working_set_bytes` |
| kubelet volume stats | Same target, path `/metrics` | `kubelet_volume_stats_capacity_bytes`, `kubelet_volume_stats_used_bytes`. Whether kind's local-path volumes report them is not verified; if they do not, PVC usage is unsupported on local |
| kube-state-metrics | A Deployment in `olf-system` with `--namespaces` limited to the OLF namespaces, `--resources` to `pods`, `deployments`, `statefulsets` and `daemonsets`, `--metric-allowlist` and `--metric-labels-allowlist=pods=[app.kubernetes.io/name,app.kubernetes.io/component]`; Roles, no ClusterRole; port 8080 | `kube_pod_container_status_restarts_total`, `kube_pod_status_ready`, `kube_pod_labels`, `kube_pod_created`, `kube_deployment_status_replicas_available`, `kube_statefulset_status_replicas_ready`, `kube_daemonset_status_number_ready` |
| Trino | Each stage's coordinator Service, port 8080, `/metrics` (OpenMetrics), static targets rendered from the stage map, basic-auth user `olf-metrics` with no password. `openmetrics.jmx-object-names` adds the memory bean to the default series (`modules/query/trino/config.tf`). Workers are not scraped | `trino_execution_name_QueryManager_RunningQueries`, `..._QueuedQueries`, `..._FailedQueries`, `..._CompletedQueries` (no `_TotalCount` suffix), `JMX_java_lang_TYPE_Memory_ATTRIBUTE_HeapMemoryUsage_used` |
| SeaweedFS | The master, volume, filer and S3 Services' `metrics` port 9327, static targets. `global.seaweedfs.monitoring.enabled` stays `false`: its ServiceMonitors need the operator's CRDs | `SeaweedFS_volumeServer_resource{name,type}`, `SeaweedFS_volumeServer_total_disk_size`, `SeaweedFS_s3_bucket_size_bytes{bucket}`, `SeaweedFS_s3_request_total{type,code,bucket}` |
| Prometheus itself | `localhost:9090` | `up` of every job, `prometheus_tsdb_head_series`, `prometheus_tsdb_storage_blocks_bytes`, `prometheus_target_scrapes_exceeded_sample_limit_total` |

Two upstream behaviours the series respect. SeaweedFS deletes a bucket's size
gauges after ten minutes without activity on that bucket, so a gap in the
storage trend reads as `unavailable`, never as zero. Trino counts per node, so
a `medium` or `large` coordinator reports only its own share.

Dagster 1.13 has no Prometheus endpoint, and `dagster-prometheus` only pushes
to a Pushgateway from user code. The two pipeline series are therefore read
from Dagster's runs by the platform API: at most 7 days and at most 500 runs
per stage, newest first. When the cap is reached the response carries
`complete: false` and `covered_from`, the oldest run's creation time, and every
step before it is `null`.

### Not collected

| Not collected | Why |
| --- | --- |
| node-exporter (host filesystem, network, load) | A host-network DaemonSet for numbers no chart needs; node CPU and memory come from the kubelet |
| `/metrics/cadvisor` | Same CPU and memory with many more series per container |
| API server, scheduler, CoreDNS, etcd | Cluster internals; no Portal chart reads them |
| Dagster, Superset, Polaris, OpenMetadata, PostgreSQL, Traefik | No endpoint at the pinned version (Dagster), or not in the Portal's chart list (#333). Their health is the service probes ([health signals](health-signals.md)) |
| Trino workers | `small` has none; added with the `medium` and `large` Trino sizes (#324) |
| Fluent Bit's own metrics | Its delivery signal is the spool probe ([ADR 0022](../adr/0022-pod-log-collection-and-archive-format.md)); its output metrics are meaningless for S3 |
| Per-pod disk on kind | Depends on the kubelet volume stats above |
| Anything on AWS or Azure | Unsupported ([ADR 0024](../adr/0024-metrics-prometheus-reference-and-read-path.md), D6) |

## Labels and limits

Scrape-time relabelling sets `stage` from maps Terraform renders: each stage
namespace to its stage and `olf-system` to `platform`; for SeaweedFS each
stage's buckets to its stage and the ops bucket to `platform`.

| Label | Value | Bound |
| --- | --- | --- |
| `stage` | `dev`, `uat`, `prod`, `platform` | Four |
| `component` | the pod's `app.kubernetes.io/name`, joined from `kube_pod_labels`; `unlabelled` when absent | The workloads of the pod census, about twenty |
| `service` | the object-store process (`master`, `volume`, `filer`, `s3`) or `trino` | Five |
| `domain` | `openlakeforge.io/domain` | Reserved: no workload sets it yet ([ADR 0022](../adr/0022-pod-log-collection-and-archive-format.md)) |

The Dagster series take `job` and, on `pipeline_runs`, the run's `status` from
the run storage; both are bounded by the stage's jobs and four statuses. The
raw kubelet and kube-state-metrics series of run pods carry the run ID in their
`pod` label (a run's Job is `dagster-run-<run_id>`). They live for the pod's
lifetime plus the staleness window, are bounded by how many pods run at once,
and are never served.

| Limit | Value | Effect |
| --- | --- | --- |
| `sample_limit` per scrape | kubelet 2,000 per node; kube-state-metrics 3,000; Trino 500 per coordinator; SeaweedFS 500 per process; self 200 | Over the limit after relabelling, the whole scrape fails, `up` turns 0 and the API answers `unavailable` |
| `label_limit` | 16 | Same failure mode |
| `label_value_length_limit` | 256 bytes | Same failure mode |
| `target_limit` per job | kubelet 20; Trino 3; SeaweedFS 4; kube-state-metrics 1 | A target explosion fails the job |
| Recording rule groups | One group per source, interval 1 minute, `limit: 500` per rule | A rule over the limit records nothing. The `kube_pod_labels` join is computed inside each per-component rule from the last two minutes of raw samples, so a pod created and deleted between two evaluations keeps its component |
| Series budget | `prometheus_tsdb_head_series` under 10,000 on the reference deployment | An operating target the e2e asserts, not a bound Prometheus enforces: `sample_limit` bounds one scrape, not the head series that accumulate as pods come and go. A deployment over it loses history to `retention.size`, not disk |

Sizing: Prometheus stores 1 to 2 bytes per sample and keeps at least three
128 MB write-ahead-log segments. 10,000 series every 30 s is 333 samples a
second, at 2 bytes about 58 MB a day: 7 days is about 0.4 GB and 30 days about
1.7 GB, plus the write-ahead log.

## Series

The platform API serves only these named series, from the catalog in
`packages/core/openlakeforge_core/metrics.py`. No route accepts a query. A
Prometheus series reads the recording rule `olf:<name>`; the two Dagster series
are derived from the stage's run storage and also exist with metrics off.

| Series | Scope | Labels | Source | Chart (#333) |
| --- | --- | --- | --- | --- |
| `cpu_usage_cores` | stage, platform | `stage`, `component` | kubelet, joined to `kube_pod_labels` | service CPU |
| `memory_working_set_bytes` | stage, platform | `stage`, `component` | kubelet, joined | service memory |
| `container_restarts` | stage, platform | `stage`, `component` | kube-state-metrics | health context |
| `node_cpu_usage_cores` | platform | none | kubelet | cluster CPU |
| `node_memory_working_set_bytes` | platform | none | kubelet | cluster memory |
| `trino_running_queries` | stage | `stage` | Trino | Trino load |
| `trino_queued_queries` | stage | `stage` | Trino | Trino load |
| `trino_failed_queries` | stage | `stage` | Trino | Trino query errors |
| `trino_heap_used_bytes` | stage | `stage` | Trino `java.lang:type=Memory` | Trino memory |
| `object_store_bucket_bytes` | stage, platform | `stage` | SeaweedFS S3, summed by stage | storage trend |
| `object_store_disk_used_bytes` | platform | `service` | SeaweedFS volume server | storage trend |
| `object_store_disk_free_bytes` | platform | `service` | SeaweedFS volume server | storage trend |
| `pipeline_runs` | stage | `stage`, `job`, `status` (`succeeded`, `failed`, `canceled`, `running`) | Dagster | job failures |
| `pipeline_run_duration_seconds` | stage | `stage`, `job` | Dagster | job duration |

Two counters are recorded as per-minute increments, which the API sums over
each step (`sum_over_time`): a counter summed across pods reads as a reset when
a pod goes, and a rule recorded once a minute holds too few samples in a 60 s
step for `increase()`.

- `container_restarts` records each minute's restarts per container from range
  selectors, so a pod deleted within the minute still counts. A container first
  seen that minute counts its whole value only if `kube_pod_created` puts its
  pod in the last two minutes; otherwise, after a scrape gap or lost history, it
  counts 0, so a gap undercounts and never replays a lifetime.
- `trino_failed_queries` records each minute's failed queries per coordinator;
  a coordinator first seen or restarted within the minute counts 0.

## Ranges and responses

A chart asks for one of four ranges, the keys of `RANGES` in
`openlakeforge_core.metrics`. There is no `start`, `end`, `step` or query
parameter. A range longer than `spec.observability.metrics.retention_days` is
refused with a 400, not answered short. The stage health routes' `range` is the
same list; there it scopes only the failed-run counts, which come from Dagster,
so the retention does not bound it.

| `range` | Window | Step | Points |
| --- | --- | --- | --- |
| `1h` | 1 hour ending now | 60 s | 60 |
| `6h` | 6 hours | 60 s | 360 |
| `24h` | 24 hours | 5 min | 288 |
| `7d` | 7 days, refused when `retention_days` is shorter | 30 min | 336 |

Every read the API sends Prometheus, the health probe and the catalog's `up`
query included, is one attempt with a 5 s deadline across the whole read, as
the service probes have: no retry after a 429, and past the deadline the answer
is `unavailable` with that reason.

The response envelope is the same for both sources:

```json
{"series": "trino_failed_queries", "stage": "dev", "source": "prometheus",
 "state": "available", "reason": "read from the metrics backend",
 "range": "6h", "step_seconds": 60, "unit": "queries",
 "observed_at": "2026-11-17T10:00:00Z", "complete": true, "covered_from": null,
 "lines": [{"labels": {"stage": "dev"},
            "points": [["2026-11-17T04:01:00Z", 0], ["2026-11-17T04:02:00Z", null]]}]}
```

A step without a sample is `null`, never `0`. `complete` is `false` only for a
Dagster series whose read reached its cap, with `covered_from` set; a
Prometheus series is always complete within retention. `state` is one of:

- `available`: one of the series' scrape targets (the stage's own, for Trino)
  is up, from one `up` query; for a Dagster series, the stage's Dagster answers.
- `unavailable`, with no `lines`: Prometheus does not answer, holds no sample
  of the series in the range, or every target behind it has `up == 0` for the
  whole range (`max_over_time(up{job=~…}[<range>])`).
- `disabled`: the profile turns metrics off.
- `not_provided`: the provider reports `metrics.supported = false`.

`reason` says which. The stage health routes' `metrics` integration takes the
same vocabulary ([health signals](health-signals.md)).

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

The workloads start with proposed requests and limits (#21), set in
`infra/terraform/modules/observability/metrics/`:

| Workload | CPU request / limit | Memory request / limit |
| --- | --- | --- |
| Prometheus | 100m / 500m | 256Mi / 768Mi |
| kube-state-metrics | 10m / 100m | 32Mi / 128Mi |

The volume per `spec.storage.size` is the `metrics` row of
`release/storage-sizes.yaml`, with `retention.size` at 75% of the volume
because compaction can briefly exceed the cap. `retention_days` defaults to 7
for every size and preset; the volume grows with the size because `medium` and
`large` run more pods, not because they need longer history.

| `spec.storage.size` | Metrics volume | `retention.size` | Fits at the full series budget |
| --- | --- | --- | --- |
| `small` | 4Gi | 3GB | about 50 days |
| `medium` | 8Gi | 6GB | about 100 days |
| `large` | 16Gi | 12GB | about 200 days |

<a id="to-be-measured"></a>

### Measured over 24 hours

Measured on 2026-11-22 and 23 (#21, slice M-f) on a local kind cluster: three
nodes, Kubernetes 1.36.1, on a 16-core AMD Ryzen AI 7 350 with 15 GiB of memory
under WSL2. The deployment was Full, `small`, metrics on, DEV and PROD, from
`main` at 5120c3b0, running 25 hours from Prometheus's start at 15:03 UTC. Each
product ran once in DEV; PROD's three product schedules ran at 02:00, 02:30 and
03:00 UTC; no e2e drill ran during the window. The sources are the footprint
check's (`check_metrics_footprint`): the kubelet stats summary every 5 minutes,
CPU as the rate of cumulative usage between samples, memory as the working set,
the volume by `du` of the data directory because kind's local-path volumes
report no stats, and `prometheus_tsdb_head_series`. Each figure is the mean and
the peak over the hour ending at the mark.

| `small`, after | Prometheus CPU | Prometheus memory | kube-state-metrics CPU | kube-state-metrics memory | Volume used | Head series |
| --- | --- | --- | --- | --- | --- | --- |
| 1 hour | 5.7m / 10.3m | 100 / 113 MiB | 1.4m / 4.2m | 50 / 56 MiB | 0.7 MiB | 1,060 |
| 6 hours | 4.3m / 7.3m | 97 / 108 MiB | 0.7m / 1.4m | 39 / 43 MiB | 4.6 MiB | 1,430 |
| 24 hours | 4.6m / 7.2m | 104 / 112 MiB | 0.9m / 1.7m | 39 / 47 MiB | 8.4 MiB | 1,023 |

Over the whole window Prometheus averaged 4.4m of CPU (peak 11.0m) and 96 MiB
(peak 125 MiB); kube-state-metrics averaged 0.8m (peak 4.3m) and 39 MiB (peak
59 MiB). Head series moved between 902 and 1,522 as blocks were compacted, a
tenth of the 10,000 budget. Neither workload restarted or was OOM-killed. The
volume grew about 8 MiB a day, so the default 7 days of history holds about
60 MiB, 2% of `small`'s 3GB `retention.size`.

Against the requests above, Prometheus uses about 5% of its CPU request and
under half of its memory request. kube-state-metrics stays under its limit but
above its 32Mi memory request. The requests and limits are not changed here:
resizing them is ADR 0024's D9 revisit.

`medium` and `large` were not run. They scrape the same targets with the same
relabelling, so their series, CPU and memory follow the number of pods and
stages, not the size; what the size changes is the volume (8Gi, 16Gi) and its
`retention.size` (6GB, 12GB). At the rate above, a 24-hour run of either would
use the same volume as `small`, far below its cap. The gap is tracked in the
[technical debt register](../technical-debt.md).
