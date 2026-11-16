# ADR 0022: Pod log collection and the log archive format

## Status

Decided, **not built**. Nothing in `infra/`, `tools/olf` or `packages/` changes
with this record: the polling CronJob and the v1 archive stay the only pod log
path until the slices under "Build order" land. What binds today is the
direction: continuous node-level collection with Fluent Bit, no Loki, one
archive format (v2) beside the v1 one the readers already know, and routing
that never consults pod metadata. Tags used below: **[V]** checked in this
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
| Bounded disk | `storage.total_limit_size` per output: at the limit the oldest queued chunk is discarded ([buffering](https://docs.fluentbit.io/manual/administration/buffering-and-storage)); `store_dir_limit_size` (0, unlimited) discards when reached ([S3 output](https://docs.fluentbit.io/manual/pipeline/outputs/s3)) | Persistent-queue size limits are not on the component page [T] |
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
| Output | `out_s3` per stage, `use_put_object On`, `compression gzip`, `total_file_size` 8M, `upload_timeout` 5m, `Retry_Limit no_limits`, `storage.total_limit_size` set | `PutObject` leaves no unfinished multipart uploads to clean; unlimited retries bound the loss to the disk limit, not to a retry count |
| Metadata | `kubernetes` filter with `Merge_Log Off`, `Annotations Off`, `Use_Kubelet Off` | App JSON stays a string in `message`; annotations can carry customer text; `nodes/proxy` is not requested |
| Telemetry | `http_server On`, `Health_Check On`, Prometheus endpoint | The indicators in the table below |

## Decision 2: failure modes and how each is detected

| Failure | Effect | Detected by | Bound |
| --- | --- | --- | --- |
| Object store unreachable or refusing credentials | Chunks queue on disk and retry | `fluentbit_output_retries_total`, `_retries_failed_total`, `_errors_total`, `fluentbit_storage_fs_chunks_down` rising; `/api/v2/health` 500 | None until the disk limit |
| Outage longer than the disk limit | **Oldest chunks discarded** | Upstream documents the discard but no metric for it; the collector's own stderr is archived with the platform logs and carries the engine's messages [T]; `fluentbit_output_dropped_records_total` [T] | `storage.total_limit_size`, sized per profile for a stated outage in hours |
| Collector restart or node reboot | Offsets in `DB`, chunks on disk and `store_dir` leftovers resume | `fluentbit_input_ingestion_paused`, restart count | A replay may repeat lines: the dedup key (Decision 4) drops them on read |
| Node replaced or collector volume lost | Unsent chunks and offsets lost | Pod restart on a new node with an empty `DB` | The node's volume; no claim stronger than that |
| Pod deleted while the collector was down | Unread lines gone with the kubelet's files | Gap between a pod's last archived line and its exit [T] | The collector's downtime |
| Burst faster than the collector reads | The kubelet rotates the file (defaults `containerLogMaxSize` 10Mi, `containerLogMaxFiles` 5 ([logging](https://kubernetes.io/docs/concepts/cluster-administration/logging/))) before it is read | None from upstream | Not bounded; named as residual loss |
| Line longer than `Buffer_Max_Size` | Skipped (`Skip_Long_Lines On`) | None from upstream [T] | Set `Buffer_Max_Size` to 1M |
| Pod gone before metadata lookup | Labels absent | `missing` lists them in the record | Fields are best effort, never routing |

Retries are unbounded and the loss point is the disk limit; that is deliberate,
because a retry count discards data on the first long outage while a disk limit
is a number an operator can size and alert on before it is reached.

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
the stage is the leading path segment, as it is for v1. The date is that of the
first record in the object (`s3_key_format` uses the first record's time
([S3 output](https://docs.fluentbit.io/manual/pipeline/outputs/s3))). There is no
hour partition: after an outage one object can hold records from earlier than
its date, so an hour would promise more than the layout guarantees. A reader of
day D lists `date=D` and `date=D-1` and filters by each record's `time` [T:
an object spanning more than a day, after a multi-day outage, is found only by a
wider read].

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
| Same response | `ArchivedLogLine` keeps `archived_at`, `namespace`, `pod`, `container`, `line`, `error`; for v2, `line` is `message` and `archived_at` is `time`. New fields (`stage`, `component`, `domain`, `product`, `run_id`, `stream`, `missing`) are additive and optional, so `olf logs` output and the API's OpenAPI document change only by additions |
| Overlap | While the CronJob and the collector both run, a pod's v1 lines are skipped when v2 records exist for that namespace, pod and container; otherwise v1 is shown. The CronJob is removed by its own change once the failed-Floe-pod acceptance of #22 passes on v2 |
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

| Slice | Content | Updates |
| --- | --- | --- |
| b | Terraform module and Helm values for the DaemonSet, the namespace map, the write-only S3 identity, NetworkPolicy flows (kube-apiserver, S3 8333), the catalog digest, failure-injection tests, per-size overhead. Whether `olf-system`'s Pod Security level admits a `hostPath` mount is [T] | ADR 0017 flow table and RBAC inventory |
| c | `LogArchive` reads v2 and dedups; fixtures from real collector output | `logs.py`, its tests |
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
