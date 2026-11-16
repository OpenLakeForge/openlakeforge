# ADR 0022: Pod log collection and the log archive format

## Status

Decided; slice b is built. The Fluent Bit DaemonSet
(`infra/terraform/modules/observability/log-collector`) writes archive format v2
beside the polling CronJob's v1, in Slim and Full alike, with a write-only key.
The CronJob and the v1 reader stay the only path the API reads until slices c
and d land. The spool age and size signals of Decision 2 are partly built (#1189): the
probe sidecar and the rules that classify its numbers exist; the platform API's
health route does not read them yet. Not built yet: the newest-key
signal of Decision 2 and the lifecycle rule of Decision 6 (`logs_archive_retention` stays reported as unsupported).
Every claim marked [T] is still untested: the kind smoke does not run here, and
slice d (#1123) verifies them on a cluster. Tags used below: **[V]** checked in this
repository or in the upstream page cited; **[T]** a claim the implementing slice
must test before relying on it.

## Context

Pod logs reach the ops bucket today through one CronJob per stage namespace:

| Fact | Where it is verified |
| --- | --- |
| `openlakeforge-k8s-log-archive` runs `*/15 * * * *` in the stage namespace, from the project-code image, and lists the namespace's pods | `_LOG_ARCHIVE_SCHEDULE`, `_log_archive_cronjob` in `tools/olf/olf/infra/deployment/activation.py` |
| Each run reads `since_seconds=3600` of every pod that still exists, so a line is written up to four times and a pod deleted between runs is never read | `packages/runtime/openlakeforge_runtime/k8s_log_archive.py` |
| Floe Jobs and Dagster run Jobs are deleted an hour after they finish | `ttl_seconds_after_finished: 3600` in `floe_profile.py`; `ttlSecondsAfterFinished = 3600` in the Dagster `release.tf` |
| The v1 object is `<base>/k8s/namespace=<ns>/date=<D>/hour=<HH>/k8s-logs-<stamp>.ndjson.gz`, a record per line carrying `schema: openlakeforge.k8s_log.v1`; `<base>` is `s3://<ops>/activations/<stage>/logs` | `k8s_log_archive.py`; `stage_log_prefixes` in `infra/terraform/environments/local/main.tf` |
| `olf logs` calls `GET /api/v1/stages/{stage}/logs`; the API's `list_logs` runs `LogArchive.read`, which lists the v1 prefix of one namespace and day and skips records of another schema | `tools/olf/olf/platform/reads.py`, `reads.py` in `packages/platform-api`, `openlakeforge_services/logs.py` |
| The API reads with `openlakeforge-platform-api-logs`: `Read` and `List` on `activations/<stage>/logs/k8s/*` | `modules/storage/seaweedfs/credentials.tf`; ADR 0017 F33 |
| `olf-system` is not archived at all | ADR 0021, build step 5 |

[#22](https://github.com/OpenLakeForge/openlakeforge/issues/22) sets the scope
and its 2026-11-16 comment fixes the direction: a lightweight collector writes
durable archives to the ops object store, Fluent Bit is the S3-compatible
candidate, Alloy qualifies only if it meets the same guarantees, and no Loki is
required. The `spec.observability.logs.archive` block is always on and carries
`retention_days` (default 30) ([deployment profile](../reference/deployment-profile.md#specobservability));
indexed search (`logs.search`) stays optional and separate.

## Decision 1: Fluent Bit 5.2.0 is the collector

Alloy does not qualify. Versions checked on 2026-11-16: Fluent Bit **5.2.0**
(GitHub release of 2026-11-09; the manual pages are not versioned, so each
behaviour marked [T] is re-tested against the pinned image) and Alloy **v1.21**
reference docs, with `grafana/alloy:v1.22.0` as the newest image.

| Guarantee | Fluent Bit | Alloy |
| --- | --- | --- |
| Object-store sink | `out_s3`: `endpoint` for S3-compatible stores, path-style, `use_put_object`, `compression gzip`, `s3_key_format` with `$TAG` and strftime ([S3 output](https://docs.fluentbit.io/manual/pipeline/outputs/s3)) | Only `otelcol.exporter.awss3`, **experimental** (needs `stability.level=experimental`), wrapping upstream `awss3` exporter v0.161.0 ([component](https://grafana.com/docs/alloy/v1.21/reference/components/otelcol/otelcol.exporter.awss3/)) |
| Record format | The record is what the pipeline builds, one JSON object per line [T] | Marshalers are `otlp_json` (OTLP batches, not a line per record), `otlp_proto`, `sumo_ic`, `body` (body only, drops the fields); the page names no NDJSON ([component](https://grafana.com/docs/alloy/v1.21/reference/components/otelcol/otelcol.exporter.awss3/)) |
| Durable buffering | `storage.type filesystem` per input with global `storage.path`; chunks are on disk before delivery ([buffering](https://docs.fluentbit.io/manual/administration/buffering-and-storage)); `out_s3` also spools files in `store_dir` and re-sends leftovers on restart ([S3 output](https://docs.fluentbit.io/manual/pipeline/outputs/s3)) | `sending_queue.storage` takes an `otelcol.storage` component for a persistent queue; the queue holds 1000 requests by default ([component](https://grafana.com/docs/alloy/v1.21/reference/components/otelcol/otelcol.exporter.awss3/)) |
| Bounded memory | `mem_buf_limit` pauses a memory-only input; `storage.max_chunks_up` (128) and `storage.backlog.mem_limit` (5M) bound chunks in memory ([buffering](https://docs.fluentbit.io/manual/administration/buffering-and-storage), [service](https://docs.fluentbit.io/manual/administration/configuring-fluent-bit/yaml/service-section)) | Queue bounded by `queue_size`; no per-input memory ceiling documented on the component page |
| Bounded disk | `storage.total_limit_size` per output: at the limit the oldest queued chunk is discarded ([buffering](https://docs.fluentbit.io/manual/administration/buffering-and-storage)), but it "isn't meaningful for S3 since it has its own buffering system"; the S3 spool is bounded only by `store_dir_limit_size`, default `0` (unlimited), which discards when reached ([S3 output](https://docs.fluentbit.io/manual/data-pipeline/outputs/s3)) | Persistent-queue size limits are not on the component page [T] |
| Retry | Per output `Retry_Limit` (default 1; `no_limits` retries forever); exponential backoff with jitter, `scheduler.base` 5 s, `scheduler.cap` 2000 s; exhausted chunks are discarded unless `storage.keep.rejected` ([retries](https://docs.fluentbit.io/manual/administration/scheduling-and-retries)) | `retry_on_failure`, `max_elapsed_time` 5m by default, `0s` retries forever ([component](https://grafana.com/docs/alloy/v1.21/reference/components/otelcol/otelcol.exporter.awss3/)) |
| Backpressure | Memory-only input pauses at `mem_buf_limit`; `storage.pause_on_chunks_overlimit` chooses between pausing and spilling to disk ([buffering](https://docs.fluentbit.io/manual/administration/buffering-and-storage)); the tail input reads from files, so a pause leaves the data in the kubelet's log file ([tail](https://docs.fluentbit.io/manual/pipeline/inputs/tail)) | `block_on_overflow` (default false) otherwise returns a retryable error at once ([component](https://grafana.com/docs/alloy/v1.21/reference/components/otelcol/otelcol.exporter.awss3/)) |
| Loss indicators | `fluentbit_output_dropped_records_total`, `_retries_total`, `_retries_failed_total`, `_errors_total`; `fluentbit_storage_fs_chunks_down`; `fluentbit_input_storage_overlimit`, `fluentbit_input_ingestion_paused`; `/api/v2/health` with `Health_Check` ([monitoring](https://docs.fluentbit.io/manual/administration/monitoring)). The pages name no metric for a chunk discarded at `storage.total_limit_size` | `otelcol_exporter_queue_size` among the debug metrics (search result for the `next` docs, not read in full) |
| Offset tracking | `DB` file of `inode=offset` per tailed file ([tail](https://docs.fluentbit.io/manual/pipeline/inputs/tail)) | Not read for this ADR |
| Image (Docker Hub, compressed, amd64) | `fluent/fluent-bit:5.2.0`, 52,269,718 bytes ([Docker Hub](https://hub.docker.com/r/fluent/fluent-bit/tags)) | `grafana/alloy:v1.22.0`, 225,009,930 bytes; `-distroless` 187,685,987 ([Docker Hub](https://hub.docker.com/r/grafana/alloy/tags)) |
| SeaweedFS | `PutObject`, multipart and `ListObjectsV2` are listed as supported; multipart is implied by `s3:PutObject` ([SeaweedFS S3 API](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)). Not yet run against `chrislusf/seaweedfs:4.48` [T] | Same store, same page; not run [T] |

The two strongest reasons: Fluent Bit has a documented S3 sink with
gzip and an `endpoint` setting for S3-compatible stores, where Alloy's is
experimental and cannot write a line-per-record format; and its image is about a
quarter of the size on a DaemonSet that runs on every node. Per-node CPU and
memory are **not** claimed here: the implementing slice measures them per
cluster size, as #22 requires.

Configuration shape, so slice b does not reopen the choice:

| Setting | Value | Why |
| --- | --- | --- |
| Input | one `tail` per routed namespace, `Path /var/log/containers/*_<namespace>_*.log`, `multiline.parser cri`, its own `DB` file, `Read_from_Head On`, `Refresh_Interval` 5, `Skip_Long_Lines On` | No `kubernetes` filter decision picks the route (Decision 3); a pod started while the collector was down is read from its first line |
| Buffering | `storage.type filesystem` on every input, `storage.path` on a `hostPath` or `emptyDir` sized by the profile | A collector restart or an S3 outage keeps the chunks |
| Output | `out_s3` per stage, `use_put_object On`, `compression gzip`, `total_file_size` 8M, `upload_timeout` 5m, **`store_dir_limit_size` set explicitly** (never the default `0`), `store_dir` on the collector volume | `PutObject` leaves no unfinished multipart uploads to clean. The S3 plugin buffers every chunk into `store_dir` and returns `FLB_OK`, so the engine's `Retry_Limit` and `storage.total_limit_size` never see an upload failure: the spool and its limit are the plugin's own ([S3 output](https://docs.fluentbit.io/manual/data-pipeline/outputs/s3), "Differences between S3 and other Fluent Bit outputs") |
| Spool bound | `store_dir_limit_size` = the stated outage budget (hours) times the stage's measured peak ingest rate (slice b measures it); the volume holds `store_dir_limit_size` plus the `storage.path` chunk budget plus headroom slice b sets | An unbounded spool fills the node disk; a bound makes the loss point a number an operator can size and alert on. Whether the spool counts uncompressed bytes, and which data is discarded at the limit (the page says only that data is discarded), is [T] |
| Upload retries | The plugin's own `retry_limit` (default 5), `file_delivery_attempt_limit` and `retry_exhausted_action` (default `quarantine`, alternative `delete`) | The page documents the defaults; a quarantined file is released from the `store_dir_limit_size` budget [V: `s3_store.c`, `s3.c` in v5.2.0], and `quarantine_dir_limit_size` defaults to `0` (unlimited), so slice a sets `retry_exhausted_action quarantine` and an explicit `quarantine_dir_limit_size` (128M) per stage: failed objects stay inspectable on the node, and once the quarantine is full a further exhausted file is deleted [V: `s3.c`]. Whether a quarantined file is retried after the outage is [T] |
| Metadata | `kubernetes` filter with `Merge_Log Off`, `Annotations Off`, `Use_Kubelet Off` | App JSON stays a string in `message`; annotations can carry customer text; `nodes/proxy` is not requested |
| Telemetry | `http_server On`, `Health_Check On`, Prometheus endpoint, plus the spool and delivery signals of Decision 2 | The engine's output metrics are "meaningless for S3" (same page), so delivery health is read from the spool and the bucket, not from them |

## Decision 2: failure modes and how each is detected

| Failure | Effect | Detected by | Bound |
| --- | --- | --- | --- |
| Object store unreachable or refusing credentials | The S3 flush buffers to `store_dir` and returns OK, so the engine sees success while files pile up and the plugin retries | **Not** the engine's output retry, error or health metrics: the page calls them "meaningless for S3". The delivery signals below | `store_dir_limit_size` |
| Outage longer than the spool limit | Data discarded at `store_dir_limit_size` (which data is [T]) | The delivery signals below; the collector's stderr carries the plugin's messages [T]; upstream documents the discard but no metric for it | `store_dir_limit_size`, sized per profile for a stated outage in hours |
| Collector restart or node reboot | Offsets in `DB`, chunks on disk and `store_dir` leftovers resume | `fluentbit_input_ingestion_paused`, restart count | A replay may repeat lines: the dedup key (Decision 4) drops them on read |
| Node replaced or collector volume lost | Unsent chunks and offsets lost | Pod restart on a new node with an empty `DB` | The node's volume; no claim stronger than that |
| Pod deleted while the collector was down | Unread lines gone with the kubelet's files | Gap between a pod's last archived line and its exit [T] | The collector's downtime |
| Burst faster than the collector reads | The kubelet rotates the file (defaults `containerLogMaxSize` 10Mi, `containerLogMaxFiles` 5 ([logging](https://kubernetes.io/docs/concepts/cluster-administration/logging/))) before it is read | None from upstream | Not bounded; named as residual loss |
| Line longer than `Buffer_Max_Size` | Skipped (`Skip_Long_Lines On`) | None from upstream [T] | Set `Buffer_Max_Size` to 1M |
| Pod gone before metadata lookup | Labels absent | `missing` lists them in the record | Fields are best effort, never routing |

The loss point is the spool limit, not a retry count: a retry count discards
data on the first long outage, while a size limit is a number an operator can
size and alert on before it is reached.

Delivery signals for the S3 output. None is an engine metric; each is
independent of the plugin's `FLB_OK`:

| Signal | Source | Reads as an outage when |
| --- | --- | --- |
| Age of the oldest file in `store_dir` | The `spool-probe` sidecar of the collector pod (below) | Older than `upload_timeout` plus one retry cycle: a healthy spool is emptied within minutes. `warning` at 10 minutes, `critical` at 30 |
| Spool size against `store_dir_limit_size` | Same sidecar (bytes under `store_dir`) | Above a fraction of the limit that leaves the stated outage budget half spent; at the limit data is already being lost. `warning` at 50% of `store_dir_limit_size`, `critical` at 90% |
| Newest object key per stage prefix | A `ListObjectsV2` by the #798 self-health probe, with a read identity (the collector's key is write-only, Decision 3) | Older than `upload_timeout` plus slack while the stage's pods are running and logging: the only signal that also covers a collector that is down |

The probe (#1189) is a second container in the collector DaemonSet's pod. It
runs the platform API image, which olf already loads into kind for the alert
evaluator, as `python -m openlakeforge_platform_api.spool_probe`, and serves
`GET /spool` on 2021: per stage, the age of the oldest file under
`spool-<stage>` (its mtime) and the bytes held, skipping the quarantine
directory, which has left the spool's budget. Why a sidecar and not an exporter
or a file: the platform API already reads Dagster, Trino and the object store
as `SERVICE` signals (`service_probes.py`), so a fourth HTTP read fits that
shape; a node-level exporter would be one more workload to deploy, and a file
would need a reader on every node. A headless Service, `log-collector-probe`,
gives the API one address per collector pod, and the API takes (in the follow-up change) the worst signal
over the pods (F42 in ADR 0017 opens that one port).

| Property | Value |
| --- | --- |
| Resources | requests 10m CPU and 32Mi; limits 100m CPU and 64Mi |
| Host access | the collector's own state directory, mounted read-only; nothing else |
| Privilege | uid 0, because the spool is root-owned; no capability, no privilege escalation, read-only root filesystem, `RuntimeDefault` seccomp |
| Credentials | none: no S3 key. It shares the pod's ServiceAccount token, which is the collector's read of pods and namespaces |
| Pod security | `olf-system` carries no Pod Security label [V: no `pod-security.kubernetes.io` in `infra/`], so the pod is admitted as the collector is; a cluster that enforces `baseline` would also refuse the collector's `hostPath` mounts, which is why the sidecar adds nothing to that decision |
| Unreadable spool | A permission or I/O error on the spool is reported as `unreadable` and both signals read `unknown`; only a file or directory that vanishes mid-scan, or a spool directory the plugin has not created yet, is tolerated and measured as empty |
| Without the image | no sidecar and no Service; the signals read as `unknown`, never `healthy` |

The thresholds are proposed defaults and are the maintainers' to review: ADR
0022 set none. They live in `openlakeforge_core.archive_health` as
`ArchiveThresholds`. Open [T] items the failure-injection e2e settles: whether
the plugin keeps appending to one file during an outage, which would hide a
stuck spool from an mtime-based age until it grows (the size signal covers it);
whether the spool counts compressed or uncompressed bytes; whether `store_dir`
and its quarantine are laid out as the probe assumes; and whether Fluent Bit's
`512M` means 512 000 000 or 536 870 912 bytes (the probe assumes the smaller,
so the fraction errs high).

Slice d (#1123) verifies them by failure injection: block the ops-bucket
endpoint with a NetworkPolicy, write known lines, and assert that the oldest-file
age and the spool size rise, that the engine's output error counters stay at zero
(which documents why they are not used), and that the newest-key age passes its
threshold; then restore the endpoint and assert the spool drains and the lines
appear in the archive. Another run fills the spool to `store_dir_limit_size` and
records which data is discarded and what the plugin logs.

## Decision 3: the trust boundary

A DaemonSet that mounts the node's `/var/log` reads the log files of **every**
namespace on its node. The collector is a privileged platform component in
`olf-system`; collection has no per-stage confidentiality, and a compromised
collector pod can read all stages' logs on its node. Isolation between stages
exists at three other points only.

| Control | What it does | What it does not do |
| --- | --- | --- |
| Routing by namespace, set in configuration | Terraform renders the stage-to-namespace map (`local.stage_namespaces`) into one `tail` input and one `out_s3` per stage. The glob `*_<namespace>_*.log` selects by the namespace segment of the file name, which the kubelet writes. Pod names follow DNS subdomain rules, which exclude `_` [V: [names](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/), by inference for pods; namespace and container names [T]], so the segment cannot be forged from a pod name | Nothing in a record, label or annotation changes the route |
| Closed world | A namespace that is in no input is not collected. A pod without a stage label, or in a namespace nobody mapped, is not routed to any stage prefix | It does not collect user-created namespaces |
| `stage` set by the collector | `record_modifier` writes `stage` from the input's own constant; `Merge_Log Off` keeps a workload from adding a `stage` key as JSON | The `stage` of a record never selects a prefix; the input does |
| Minimum write scope | The collector's S3 identity gets `Write` on `<ops>/activations/<stage>/logs/v2/*` for each stage and on the platform prefix, and nothing else: no `Read`, no `List`, no bucket scope (the `Write:<bucket>/<prefix>*` form of `credentials.tf`) | It is one key for all stages, so its scope cannot stop a misconfigured output from writing to the wrong stage. SeaweedFS 4.48 also gates overwrite and delete on `Write`, so the archive is not append-only (the same limit as the audit key in ADR 0021) |
| Cluster RBAC | `get`, `list`, `watch` on `pods` and `namespaces`, as the filter documents ([kubernetes filter](https://docs.fluentbit.io/manual/pipeline/filters/kubernetes)); no `nodes/proxy`, no write verbs | A cluster-wide read of every pod spec |
| Readers | The platform API's role matrix and its read key decide who reads a stage's prefix (ADR 0021); a DEV caller cannot name a PROD prefix because `stage` is the path segment the matrix checks | The collector plays no part in read authorization |

Why mislabelled or absent stage metadata cannot send DEV lines to PROD: the
route is the input, and the input is chosen by the file name. The `kubernetes`
filter runs after routing and can only add fields. A DEV pod labelled
`openlakeforge.io/stage=prod` is still a `dev` record under the `dev` prefix. The
slice that builds it ships the test #22 asks for: a pod with a wrong label, a
pod with none, and a workload printing `{"stage":"prod"}` all land under their
own stage.

The platform namespaces (`olf-system`) are collected into
`<ops>/platform/logs/v2/`, a prefix that is not under any stage. Who may read it
is decided with the API slice, not here.

## Decision 4: archive format v2

NDJSON, gzip, one object per flush, written by the collector. Open, line-oriented,
readable with `zcat`.

Partitioning: `<base>/v2/namespace=<ns>/date=<YYYY-MM-DD>/<name>.ndjson.gz`,
where `<base>` is the stage's existing `s3://<ops>/activations/<stage>/logs`, so
the stage is the leading path segment, as it is for v1. The date is the **UTC date
of every record in the object**, not the time of the flush or of the first
record. `s3_key_format` offers `$UUID`, `$INDEX`, `$TAG`, `$TAG[n]` and strftime
time, with the time being the first record's ([S3 output](https://docs.fluentbit.io/manual/data-pipeline/outputs/s3)); the page names no specifier that reads a record field,
and after an outage the spool uploads old files after new ones, so a first-record
date alone can put day D's records under D+1. The date therefore goes into the
tag: a `rewrite_tag` filter re-tags each record `<namespace>.<YYYY-MM-DD of its
own time>`, and the key is
`v2/namespace=$TAG[0]/date=$TAG[1]/<stage>-$UUID.ndjson.gz`. The plugin keeps one
buffer per tag, so an object holds one date by construction, and a reader of day
D lists `date=D` only and still filters by each record's `time`. Namespace names
contain no dot, so `$TAG[0]` is the namespace. Whether `rewrite_tag` can build a
tag from the `time` field (a record accessor, run after the record carries the
CRI time) and how many open per-tag buffers it costs on a busy day boundary is
[T], slice b's first test. If it cannot, the fallback is a bounded read: day D is
listed in `date=D` through `date=D+k`, with `k = ceil((outage budget +
upload_timeout) / 24h)`, which holds only if the spool discards its oldest data first [T]; the reader never lists fewer days than that. There
is no hour partition: an hour would promise a precision the fallback does not
keep.

```text
s3://openlakeforge-ops/activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/dev-4f1b6c2e-9a3d.ndjson.gz
s3://openlakeforge-ops/platform/logs/v2/namespace=olf-system/date=2026-11-16/platform-0b7e5d11-62aa.ndjson.gz
```

Record fields:

| Field | Source | Notes |
| --- | --- | --- |
| `schema` | constant | `openlakeforge.k8s_log.v2`; readers skip other values, as `logs.py` does |
| `time` | the CRI timestamp, UTC, nanoseconds | The runtime's clock, not the collector's |
| `stage` | the input | `dev`, `uat`, `prod`, or `platform` |
| `namespace`, `pod`, `container` | the log file name | Always present |
| `container_id` | the log file name | Distinguishes a restarted container |
| `stream` | CRI | `stdout` or `stderr` |
| `component` | label `app.kubernetes.io/name` | Best effort |
| `domain`, `product` | labels `openlakeforge.io/domain`, `openlakeforge.io/product` | Best effort. **No workload sets these labels today** [V: grep of the tree]; the pipeline slices add them to Floe and dbt pods |
| `run_id` | Dagster's run label or the `dagster-run-<id>` Job name | Best effort [T: neither is verified in this tree; the slice that reads it confirms which one Dagster sets] |
| `message` | the log line | A string, never merged into the record |
| `missing` | the filter | Array naming each best-effort field it could not fill, so absence is visible |
| `dedup_key` | `<container_id>:<time>:<offset>` | `offset` from the tail input's `Offset_Key` ([tail](https://docs.fluentbit.io/manual/pipeline/inputs/tail)); the offset's exact meaning is [T], what matters is that a replay of one line produces the same key |

```json
{"schema":"openlakeforge.k8s_log.v2","time":"2026-11-16T09:41:07.123456789Z","stage":"dev","namespace":"olf-dev","component":"floe","pod":"floe-sales-x7k2p","container":"runner","container_id":"3f9c0a41d5e2","stream":"stderr","domain":"sales","product":null,"run_id":"8f1c2d4e-0b7a-4c55-9a10-2f6e7a3b9c01","message":"2026-11-16 09:41:07 ERROR contract violated: orders.amount","missing":["product"],"dedup_key":"3f9c0a41d5e2:2026-11-16T09:41:07.123456789Z:48213"}
```

Run-scoped retrieval does not need a pod name: `run_id` is in the record, and
the API's filter reads the day's objects and selects on it.

## Decision 5: reader migration

`LogArchive.read` (`openlakeforge_services/logs.py`) is the one reader behind
`olf logs` and the API's `list_logs`. It does not change shape; it learns a
second layout.

| Rule | Behaviour |
| --- | --- |
| v1 stays | `<base>/k8s/...` is read exactly as today. v1 objects are never rewritten; the reader keeps the v1 path as long as any can exist, which is at least `retention_days` after the CronJob is removed |
| v2 added | `<base>/v2/...` is listed in the same call. The v1 reader never lists `v2/`, so an old API or CLI keeps working while v2 objects accumulate |
| Same response | `ArchivedLogLine` keeps `archived_at`, `namespace`, `pod`, `container`, `line`, `error`; for v2, `line` is `message` and `archived_at` is `time`. New fields (`stage`, `component`, `domain`, `product`, `run_id`, `stream`, `missing`, `source`) are additive and optional, so `olf logs` output and the API's OpenAPI document change only by additions |
| Overlap | While the CronJob and the collector both run, a v1 line is dropped only when v2 holds a record with the same namespace, pod, container, timestamp and text; every unmatched v1 line is kept, including a container's history from before the collector's first record. The v1 `line` begins with the kubelet timestamp (`timestamps=True` in `k8s_log_archive.py` [V]); that it equals the CRI `time` byte for byte is [T], and where it does not the line is shown from both sources, with `source` (`v1` or `v2`) set on each so a duplicate is visible, never a loss. The CronJob is removed by its own change once the failed-Floe-pod acceptance of #22 passes on v2 |
| Duplicates | Within v2, a record whose `dedup_key` was already returned is dropped |
| Authorization | Unchanged: `stage` stays the path segment the permission matrix checks. The read key widens from `logs/k8s/` to `logs/` of each stage, which changes ADR 0017 F33 and ADR 0021 build step 3 in the same slice |

## Decision 6: pinning

AGENTS.md rule 6 applies. The implementing slice adds
`fluent/fluent-bit:5.2.0@sha256:<index digest>` under `components.images` in
`release/component-catalog.yaml`, beside the SeaweedFS line, and
`olf check components` enforces it; this record names the version, the PR
records the digest, as ADR 0017 does for Calico. The collector image is loaded
into kind with the existing prefetch path. Retention (`retention_days`) becomes a
lifecycle rule on the `v2/` prefixes through the ops bucket contract; whether
SeaweedFS 4.48 honours bucket lifecycle rules is [T].

## Build order

Slice b as built deviates from the sketches above in four places, all inside the
decisions: the tag is `<namespace>.<date>` and the `Match_Regex` of each stage's
output names the namespace, so one tag carries the namespace and date as
Decision 4 says; a Lua filter builds the record from the file name (`Path_Key`),
the CRI timestamp (`time_as_table`, nanoseconds) and the filter's labels, and
writes a fresh table so a workload's own keys cannot enter it; an absent label is
left out and named in `missing` rather than written as `null`; the offsets, engine
chunks and S3 spool sit on a `hostPath` under `/var/lib/openlakeforge/log-collector`,
bounded per stage by `store_dir_limit_size` (512M) plus `quarantine_dir_limit_size`
(128M), which are separate budgets because a quarantined file leaves the spool
budget, and `storage.total_limit_size` (256M) for engine chunks, so a replaced pod
keeps its offsets. Whether the `cri` multiline parser sets
the event time from the CRI timestamp, whether `Offset_Key` is the offset of the
line, and whether the S3 plugin creates `store_dir` itself are [T]. `olf-system`
admits the `hostPath` mounts: no namespace carries a Pod Security label [V].

| Slice | Content | Updates |
| --- | --- | --- |
| b | Terraform module and Helm values for the DaemonSet, the namespace map, the write-only S3 identity, NetworkPolicy flows (kube-apiserver, S3 8333), the catalog digest, failure-injection tests, per-size overhead. Whether `olf-system`'s Pod Security level admits a `hostPath` mount is [T] | ADR 0017 flow table and RBAC inventory |
| c | `LogArchive` reads v2 and dedups, filters by run, product, component, severity and time; the API's `list_logs` role matrix row limits `data-engineer` to DEV; the reader key gains `v2/`. The CronJob and v1 stay (#1122) | `logs.py`, its tests |
| d | API filters by stage, product, run, component; `olf logs` flags; CronJob removal; documentation of retention and the runbook | ADR 0021, `docs/reference/cli.md`, `docs/technical-debt.md` (the observability row still names Loki) |

## Rejected

| Option | Why not |
| --- | --- |
| Alloy with `otelcol.exporter.awss3` | Experimental, and no documented line-per-record format |
| One DaemonSet per stage | The only way to give each stage its own write key, at the cost of a collector per stage per node; the key scope would still not limit what a pod on the node can read |
| Routing by pod label | A label is set by whoever creates the pod |
| Keep polling, shorten the interval | Short-lived pods still vanish between runs and the overlap stays |
| Loki | Not required by #22; an indexed backend is the optional #795 |

## History

2026-11-16: created for #1120 (slice a of #22).

2026-11-16: slice b (#1121) built the collector module, the write-only identity,
the catalog pin (`fluent_bit`, index digest read from Docker Hub for 5.2.0) and
the F37 flows; Status and Build order updated, no decision changed.

2026-11-16: slice c (#1122): reader, API filters and the reader key land. `severity` is read from the
level token in `message`, because the v2 record has no severity field.

2026-11-16: #1189 (part): the `spool-probe` sidecar, `archive_health` in
`openlakeforge_core` and the F42 flow land, with proposed thresholds recorded in
Decision 2. The health route reading them, the newest-key signal and the
failure-injection e2e follow in their own changes.
