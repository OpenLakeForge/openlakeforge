# ADR 0022: Pod log collection and the log archive format

## Status

Decided; slice b is built. The Fluent Bit DaemonSet
(`infra/terraform/modules/observability/log-collector`) writes archive format v2
beside the polling CronJob's v1, in Slim and Full alike, with a write-only key.
The CronJob and the v1 reader stay the only path the API reads until slices c
and d land. The three delivery signals of Decision 2 are built (#1189): the probe sidecar,
the platform API's stage health route that reads it and lists the newest object,
and the thresholds. Their failure-injection e2e (`_log_delivery.py`) is written
but has not been run on a cluster (see Decision 2), so every [T] item it covers
stays [T]. The retention of Decision 6 is built for local (#22): see the History entry for how it differs from a lifecycle rule.
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
| Output | `out_s3` per stage, `use_put_object On`, `compression gzip`, `total_file_size` 8M, `upload_timeout` 5m, **`store_dir_limit_size` set explicitly** (never the default `0`), `store_dir` on the collector volume | `PutObject` leaves no unfinished multipart uploads to clean. The S3 plugin buffers chunks into `store_dir`, so the spool and its limit are the plugin's own ([S3 output](https://docs.fluentbit.io/manual/data-pipeline/outputs/s3), "Differences between S3 and other Fluent Bit outputs"). That holds while the store answers fast (refused or unroutable). When the store drops packets, each upload attempt holds the output's flush for two `net.connect_timeout` periods; intake to `store_dir` then falls below the ingest rate and records overflow the re-tag emitter before the spool (Decision 2). `net.connect_timeout` is 2 s to keep that window short [V: #1123 reproduction against the pinned image] |
| Spool bound | `store_dir_limit_size` = the stated outage budget (hours) times the stage's measured peak ingest rate (slice b measures it); the volume holds `store_dir_limit_size` plus the `storage.path` chunk budget plus headroom slice b sets | An unbounded spool fills the node disk; a bound makes the loss point a number an operator can size and alert on. Whether the spool counts uncompressed bytes, and which data is discarded at the limit (the page says only that data is discarded), is [T] |
| Upload retries | The plugin's own `retry_limit` (default 5), `file_delivery_attempt_limit` and `retry_exhausted_action` (default `quarantine`, alternative `delete`) | The page documents the defaults; a quarantined file is released from the `store_dir_limit_size` budget [V: `s3_store.c`, `s3.c` in v5.2.0], and `quarantine_dir_limit_size` defaults to `0` (unlimited), so slice a sets `retry_exhausted_action quarantine` and an explicit `quarantine_dir_limit_size` (128M) per stage: failed objects stay inspectable on the node, and once the quarantine is full a further exhausted file is deleted [V: `s3.c`]. Whether a quarantined file is retried after the outage is [T] |
| Metadata | `kubernetes` filter with `Merge_Log Off`, `Annotations Off`, `Use_Kubelet Off` | App JSON stays a string in `message`; annotations can carry customer text; `nodes/proxy` is not requested |
| Telemetry | `http_server On`, `Health_Check On`, Prometheus endpoint, plus the spool and delivery signals of Decision 2 | The engine's output metrics are "meaningless for S3" (same page), so delivery health is read from the spool and the bucket, not from them |

## Decision 2: failure modes and how each is detected

| Failure | Effect | Detected by | Bound |
| --- | --- | --- | --- |
| Object store unreachable or refusing credentials | The S3 flush buffers to `store_dir` while files pile up and the plugin retries. The engine's `errors` and `dropped_records` counters for the output still rise during the outage, because a failed upload attempt returns an error after its chunk is already in the spool: they count failed attempts, not lost records [V: #1123 e2e and reproduction] | **Not** the engine's output retry, error or health metrics: the page calls them "meaningless for S3", and they rise without loss. The delivery signals below | `store_dir_limit_size` |
| Outage longer than the spool limit | Data discarded at `store_dir_limit_size` (which data is [T]) | The delivery signals below; the collector's stderr carries the plugin's messages [T]; upstream documents the discard but no metric for it | `store_dir_limit_size`, sized per profile for a stated outage in hours |
| Store drops packets while ingest is fast | Each upload attempt holds the S3 output for two connect timeouts, so the output takes in less than the stage ingests and the excess is lost at the re-tag emitter (next row) before `store_dir_limit_size` is reached. Measured at 5000 lines/s of 1 KB: about 95% lost at the 10 s default connect timeout, 20% at 2 s | None from upstream; the spool signals show the spool filling slower than ingest | `net.connect_timeout 2`; accepted as residual loss for bursts during such an outage |
| Re-tag emitter over `Emitter_Mem_Buf_Limit` | The record keeps its input tag, which no output matches, and is discarded with no error and no counter | Line counts in the e2e (footprint and spool-limit drills) only | One `Flush` interval of ingest stays under the emitter limit: `Flush 1`, `Emitter_Mem_Buf_Limit 20M`, measured lossless at about 6 MB/s with the store reachable. An output stalled by the previous row still reaches it |
| Collector restart or node reboot | Offsets in `DB`, chunks on disk and `store_dir` leftovers resume | `fluentbit_input_ingestion_paused`, restart count | A replay may repeat lines: the dedup key (Decision 4) drops them on read |
| Node replaced or collector volume lost | Unsent chunks and offsets lost | Pod restart on a new node with an empty `DB` | The node's volume; no claim stronger than that |
| Pod deleted while the collector was down | Unread lines gone with the kubelet's files | Gap between a pod's last archived line and its exit [T] | The collector's downtime |
| Burst faster than the collector reads | The kubelet rotates the file (defaults `containerLogMaxSize` 10Mi, `containerLogMaxFiles` 5 ([logging](https://kubernetes.io/docs/concepts/cluster-administration/logging/))) before it is read | None from upstream | Not bounded; named as residual loss |
| Line longer than `Buffer_Max_Size` | Skipped (`Skip_Long_Lines On`) | None from upstream [T] | Set `Buffer_Max_Size` to 1M |
| Pod gone before metadata lookup | Labels absent | `missing` lists them in the record | Fields are best effort, never routing |

The loss point of a long outage is the spool limit, not a retry count: a retry
count discards data on the first long outage, while a size limit is a number an
operator can size and alert on before it is reached. It is not the only loss
point: under an outage that drops packets, ingest above the S3 output's reduced
intake is lost at the `rewrite_tag` emitter before the spool limit is reached,
silently (rows above). This is accepted and named as residual loss. Letting the
tail input pause instead (no emitter limit), which would move the loss to the
kubelet's log rotation, needs the collector's memory limit re-measured first:
it reached about 250 MiB of its 256Mi limit in the reproduction.

Delivery signals for the S3 output. None is an engine metric; each is
independent of the plugin's `FLB_OK`:

| Signal | Source | Reads as an outage when |
| --- | --- | --- |
| Age of the oldest file in `store_dir` | The `spool-probe` sidecar of the collector pod (below) | Older than `upload_timeout` plus one retry cycle: a healthy spool is emptied within minutes. `warning` at 10 minutes, `critical` at 30 |
| Spool size against `store_dir_limit_size` | Same sidecar (bytes under `store_dir`) | Above a fraction of the limit that leaves the stated outage budget half spent; at the limit data is already being lost. `warning` at 50% of `store_dir_limit_size`, `critical` at 90% |
| Newest object key per stage prefix | A `ListObjectsV2` by the #798 self-health probe, with a read identity (the collector's key is write-only, Decision 3) | Older than `upload_timeout` plus slack while the stage's pods are running and logging: the only signal that also covers a collector that is down |

The newest object's `LastModified` is the stage's last confirmed durable
write. The platform API serves it as `last_success_at` on the
`log-archive.newest-object` service row ([ADR 0021](0021-platform-api.md),
decision 7), null when the listing found no object in the days it looked at or
could not list, so a client never shows a write that was not seen.

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
gives the API one address per collector pod, and the API asks the pods together, each within 60% of the probe timeout, and takes the worst signal
over the pods (`critical` and `warning` outrank `unknown`, so a silent pod never hides a reachable one's reading) (F42 in ADR 0017 opens that one port).

| Property | Value |
| --- | --- |
| Resources | requests 10m CPU and 32Mi; limits 100m CPU and 64Mi |
| Host access | the collector's own state directory, mounted read-only; nothing else |
| Privilege | uid 0, because the spool is root-owned; no capability, no privilege escalation, read-only root filesystem, `RuntimeDefault` seccomp |
| Credentials | none: no S3 key. It shares the pod's ServiceAccount token, which is the collector's read of pods and namespaces |
| Pod security | `olf-system` carries no Pod Security label [V: no `pod-security.kubernetes.io` in `infra/`], so the pod is admitted as the collector is; a cluster that enforces `baseline` would also refuse the collector's `hostPath` mounts, which is why the sidecar adds nothing to that decision |
| Unreadable spool | A permission or I/O error on the spool is reported as `unreadable` and both signals read `unknown`; only a file or directory that vanishes mid-scan, or a spool directory the plugin has not created yet, is tolerated and measured as empty |
| Pods the API cannot see | The headless Service publishes not-ready pods, so a collector that is up but unready is asked, and one that does not answer makes the signals `unknown`. A pod with no IP yet (Pending) is in no DNS answer, and the API cannot list `olf-system` pods to notice it; that gap is out of scope here, and the newest-object signal still sees a stage whose archive stops |
| Without the image | no sidecar and no Service; the signals read as `unknown`, never `healthy` |

The maintainer accepted the thresholds as proposed on 2026-11-18: spool age
`warning` at 10 minutes and `critical` at 30, spool size `warning` at 50% and
`critical` at 90% of `store_dir_limit_size`, newest object `warning` at 15
minutes and `critical` at 60. They live in `openlakeforge_core.archive_health`
as `ArchiveThresholds`. Open [T] items the failure-injection e2e settles: whether
the plugin keeps appending to one file during an outage, which would hide a
stuck spool from an mtime-based age until it grows (the size signal covers it);
whether the spool counts compressed or uncompressed bytes; whether `store_dir`
and its quarantine are laid out as the probe assumes; and whether Fluent Bit's
`512M` means 512 000 000 or 536 870 912 bytes (the probe assumes the smaller,
so the fraction errs high).

The newest-key signal reuses an identity: the platform API's log reader,
`openlakeforge-platform-api-logs`, already holds `List` on each stage's
`activations/<stage>/logs/v2/` (`credentials.tf`; F33). The alert evaluator's
key was checked and cannot: it lists `alerts/` and each stage's quality
manifests only. No identity is minted, no grant widened, no flow added. The
health route lists `logs/v2/namespace=<namespace>/date=<day>/` for today and
yesterday (UTC), recursively, so the hour partitions are included, and takes the newest `LastModified`, with a second S3 client on
the same key and the probe's timeouts (the API's log client waits up to 30 s).
Limits: the `platform/logs/v2/` prefix is outside that grant, so the platform
namespace has the spool signals only; at most 5000 objects of a day are looked
at, so a runaway prefix errs towards a stale reading; and a stage whose pods log
nothing reads like a stalled collector (`unknown` when no object exists in the
two days, `warning` at 15 minutes, `critical` at 60). These thresholds were
accepted with the spool's.

The failure-injection e2e (#1189, `tools/olf/olf/infra/e2e/_log_delivery.py`,
two `drill` assertions of the local run) reads the signals as the health route
does, from the probe fetched from a platform API pod (the path F42 admits; the API
server's pod proxy would be refused) and from a listing of the stage's prefix,
with the production thresholds. It cuts the route by replacing
the collector's own NetworkPolicy, `olf-flows-log-collector`, with a copy that
lacks the S3 egress rule, and puts the original back in a `finally`: policies
are additive, so an extra policy could not take an allowed flow away. Run 1:
baseline healthy, then known lines under the cut, then it waits for the spool
age and the newest object to pass `warning` (up to 40 minutes), asserts bytes
are spooled and records the engine's `s3` failure counters (they rise during the
outage, see the first row of the table above), restores the route and waits for
every signal to be `healthy` and every file that was in a spool when the route
came back to have left it (a stage whose pods keep logging always holds the
buffer of the current `upload_timeout` window, so an empty spool is not the
drained state), then for the lines to be in the archive exactly once. Run 2: fills the spool past `store_dir_limit_size` with
numbered lines, asserts the size signal turns `critical` (a fill Job that fails
ends that wait at once: it has stopped writing), that the spool peak
stays within one 8M file plus 8M of the limit and that the collector does not
restart, then waits for the emitter Job to finish (a failed or overdue one is an error, so
the loss is measured against every planned line), for the files spooled at the restore to leave the spool and the archive to stop changing, and
records the plugin's messages and which line numbers the archive lacks. Run 2 records and does not assert which data is
discarded, because the plugin page does not say. The size signal reaches
`warning` only in run 2 (a few kilobytes never reach half of 512M).

Slice d (#1123) verifies them by failure injection: block the ops-bucket
endpoint with a NetworkPolicy, write known lines, and assert that the oldest-file
age and the spool size rise and that the newest-key age passes its threshold,
recording the engine's output error counters (they rise without loss, which
documents why they are not used); then restore the endpoint and assert the spool drains and the lines
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

The `spool-probe` sidecar (Decision 2) runs as uid 0, because the spool files
the collector writes are root-owned; the maintainer accepted that on
2026-11-18. The probe has no Linux capability, no privilege escalation, a
read-only root filesystem, `RuntimeDefault` seccomp and no S3 key. Its one host
path is the collector's state directory, mounted read-only. Root in that
container can read the spooled lines of every stage on the node, which the
collector already holds, so the probe stays inside the collector's trust
boundary and does not widen it.

The platform namespaces (`olf-system`) are collected into
`<ops>/platform/logs/v2/`, a prefix that is not under any stage. Who may read it
is decided with the API slice, not here.

## Decision 4: archive format v2

NDJSON, gzip, one object per flush, written by the collector. Open, line-oriented,
readable with `zcat`.

Partitioning: `<base>/v2/namespace=<ns>/date=<YYYY-MM-DD>/hour=<HH>/<name>.ndjson.gz`,
where `<base>` is the stage's existing `s3://<ops>/activations/<stage>/logs`, so
the stage is the leading path segment, as it is for v1. The date and hour are the
**UTC date and hour of every record in the object**, not the time of the flush or
of the first record. `s3_key_format`'s strftime time is not the records' time: in
Fluent Bit 5.2.0 it is the first record's time for a fresh buffer, but the restart
time for a file restored from `store_dir` (`s3_store.c`, `s3.c` `put_all_chunks`)
[V], so after an outage it would name the wrong hour. The key therefore takes no
strftime time. The record builder writes the record's `date` and `hour`, a
`rewrite_tag` filter re-tags each record `<namespace>.<date>.<hour>`, and the key
is `v2/namespace=$TAG[0]/date=$TAG[1]/hour=$TAG[2]/<stage>-$UUID.ndjson.gz`. The
plugin keeps one buffer per tag, and a restored file keeps its tag [V], so an
object holds one hour by construction, however late it is uploaded. A reader of a
window lists only the `hour=` partitions the window touches, with no slack hour,
and still filters by each record's `time`. Namespace names contain no dot, so
`$TAG[0]` is the namespace. Objects written before hour partitions sit directly
under `date=` and are read whole, with the upload-time skip, as is any sub-prefix
of a day that is not an hour (a collector upgraded with date-only tags on disk
writes `hour=<namespace>.<date>[2]/`, because the plugin substitutes the whole tag
for a missing part).

```text
s3://openlakeforge-ops/activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/hour=09/dev-4f1b6c2e.ndjson.gz
s3://openlakeforge-ops/platform/logs/v2/namespace=olf-system/date=2026-11-16/hour=09/platform-0b7e5d11.ndjson.gz
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
| Same response | `ArchivedLogLine` keeps `archived_at`, `namespace`, `pod`, `container`, `line`, `error`; for v2, `line` is `message` and `archived_at` is `time`. New fields (`stage`, `component`, `domain`, `product`, `run_id`, `stream`, `missing`, `source`) are additive and optional, so `olf logs` output and the API's OpenAPI document change only by additions. `timestamp` (v2 `time`, or the kubelet timestamp a v1 line opens with) and `level` (read by `level_of` from a bounded set of message shapes) are additive and null when the source recorded none |
| Overlap | While the CronJob and the collector both run, a v1 line is dropped only when v2 holds a record with the same namespace, pod, container, timestamp and text; every unmatched v1 line is kept, including a container's history from before the collector's first record. The v1 `line` begins with the kubelet timestamp (`timestamps=True` in `k8s_log_archive.py` [V]); that it equals the CRI `time` byte for byte is [T], and where it does not the line is shown from both sources, with `source` (`v1` or `v2`) set on each so a duplicate is visible, never a loss. The CronJob is removed by its own change once the failed-Floe-pod acceptance of #22 passes on v2 |
| Duplicates | Within v2, a record whose `dedup_key` was already returned is dropped |
| Authorization | Unchanged: `stage` stays the path segment the permission matrix checks. The read key widens from `logs/k8s/` to `logs/` of each stage, which changes ADR 0017 F33 and ADR 0021 build step 3 in the same slice |

## Decision 6: pinning

AGENTS.md rule 6 applies. The implementing slice adds
`fluent/fluent-bit:5.2.0@sha256:<index digest>` under `components.images` in
`release/component-catalog.yaml`, beside the SeaweedFS line, and
`olf check components` enforces it; this record names the version, the PR
records the digest, as ADR 0017 does for Calico. The collector image is loaded
into kind with the existing prefetch path. Retention (`retention_days`) is
applied to the `v2/` prefixes by the object store and reported through the ops
bucket contract (`retention_supported`). On local it is a SeaweedFS filer path
TTL (`fs.configure -ttl`) on each prefix, set by a Job of
`modules/storage/seaweedfs`, because SeaweedFS 4.48 stores an S3 lifecycle rule
without creating a TTL from it (History, 2026-11-17). A path TTL counts 1 to 255
of a unit (`m`, `h`, `d`, `w`, `M` of 30 days, `y` of 365 days) [V:
`weed/shell/command_fs_configure.go`, `weed/storage/needle/volume_ttl.go` at
4.48], so up to 255 days the rule is in days and above that it is the shortest
whole number of weeks, months or years that is at least `retention_days`, the
larger unit on a tie: exact where the days divide (365 is `1y`, 3650 is `10y`),
otherwise up to 6 days longer (weeks, to 1785 days) or 29 days longer (months),
never shorter. AWS and Azure apply none yet (`retention_supported` is `false`).
The rule reaches only objects written after it is set.

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
keeps its offsets. The `rewrite_tag` emitter is a loss point of its own
(Decision 2): the service `Flush` is 1 s and `Emitter_Mem_Buf_Limit` 20M so one
flush interval of ingest fits it, and each S3 output sets `net.connect_timeout 2`
so a store that drops packets stalls the output for seconds, not tens of seconds. Whether the `cri` multiline parser sets
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
Decision 2. A spool the probe cannot read is `unknown`, not empty.

2026-11-16: #1189 (part): the stage health route reads the spool signals from
every collector pod and lists the newest archive object with the existing log
reader key (no new identity). The failure-injection e2e follows.

2026-11-16: #1189 (part): the failure-injection e2e is written (not yet run on a
cluster); `parse_spool_report` moves to `openlakeforge_core` so the e2e and the
API read the probe's answer with one parser.

2026-11-17: #22 close-out. The polling CronJob is no longer rendered where the
collector runs (local); AWS and Azure have no collector, so they keep it as
their only capture, and the reader reads both layouts. Retention is applied on
local as a SeaweedFS filer path TTL (`fs.configure -ttl`) on each v2 prefix:
in a SeaweedFS 4.48 container, `PutBucketLifecycleConfiguration` was accepted and
read back, but `fs.configure` then listed no path rule, so nothing the rule names
is given a TTL; a `1m` path rule expired an object within a minute and left an
object outside its prefix alone. What is built is therefore a path TTL, not the
lifecycle rule Decision 6 names, and it is reported through the same contract
fields (`retention_days`, `retention_supported`). Whether the S3 rule would
expire anything after its first day was not waited out. The path TTL reaches
only objects written after it is set.

2026-11-18: #1123 runs the failure-injection e2e on a cluster. Decision 2 is
corrected, no threshold changed: the engine's S3 failure counters rise during an
outage without loss, so the drill records them; the drain waits for the files
spooled at the restore, not an empty spool; and the `rewrite_tag` emitter is
named as a second, silent loss point, which an outage that drops packets reaches
before the spool limit. `Flush 1`, `Emitter_Mem_Buf_Limit 20M` and
`net.connect_timeout 2` bound it; the residual loss is accepted.

2026-11-18: the maintainer accepts the delivery-signal thresholds of Decision 2
as proposed (spool age 10 and 30 minutes, spool size 50% and 90% of the limit,
newest object 15 and 60 minutes) and the spool probe's uid 0 (Decision 3); no
value or setting changed.

2026-11-18: Decision 6 now names the path TTL that binds on local instead of a
lifecycle rule, with `fs.configure`'s limit of 255 per unit: above 255 days the
TTL is whole weeks, months or years, rounded up when the days do not divide
(#1248 review).

2026-11-18: run 2 of the failure-injection e2e stops waiting for the spool to
fill as soon as its fill Job fails, instead of after 30 minutes. A fill that
wrote nothing (BusyBox awk rejecting `%*s`, fixed in #1264) had read as a
collector that never spooled it; no threshold or setting changed.

2026-11-18: Decision 2 states that the newest archive object's write time is
exposed, as `last_success_at` on the `log-archive.newest-object` service row
of the platform API (#798); no threshold or setting changed.

2026-11-20: #1326. Each line carries `timestamp` and `level`; `severity` filters on that `level`, which is read only from a JSON level key, a leading level word, Python logging's format or a top-level logfmt `level=` (a quoted value's text never counts), not from a level word anywhere in the line. `LogArchive.read_days` reads a run's window across UTC days, ordering lines by a deterministic position (time, layout, day, object, record) a cursor continues from, and lists v1 partitions `V1_LOOKBACK_SECONDS` (`openlakeforge_core/log_archive.py`) past the window's end, because v1 objects are partitioned by archive time. The archive format is unchanged.

2026-11-20: #1326. Decision 4 gains an hour partition: the collector tags each record `<namespace>.<date>.<hour>`
of its own time and keys the object `date=$TAG[1]/hour=$TAG[2]/`, because a run's read had to list and open every
object of the day, including hours of spooled lines uploaded during a short run (full e2e run 44754438238 on #1338
answered 502). The key holds no strftime time, which Fluent Bit 5.2.0 takes from the restart for a file left in the
spool. The reader lists only the hours a window touches, with no slack hour, since an object holds one hour by
construction. Objects already written directly under `date=` are still read, with the upload-time skip. The record
format is unchanged.

2026-11-20: #1347. `LogArchive.read_days` takes `text`, a case-insensitive plain substring matched against each line's message (a v1 line without the kubelet timestamp it opens with, so a v1 copy of a v2 line matches exactly when the v2 record does and Decision 5's overlap rule is unchanged; an archive error record has no message). It is a filter like `severity`, applied before the ordering, the dedup and `limit`, so a cursor's position is that of the last matching line and paging neither skips nor repeats a match. The search text is never logged. The archive format is unchanged.
