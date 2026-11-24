# Log archive

How pod logs reach the ops bucket, how long they stay, how to read them, and what to do when delivery stops. It applies
to the local provider, where a node collector writes the archive.

## What is collected

A Fluent Bit DaemonSet (`log-collector`, namespace `olf-system`) reads every container's log file on its node and
writes gzipped NDJSON to the ops bucket. It collects the stage namespaces and `olf-system`, nothing else, and routes
by the namespace in the log file's name, never by a pod label ([ADR 0022](../../adr/0022-pod-log-collection-and-archive-format.md)).
A node-level collector can read every namespace on its node, so it is a platform component and makes no claim of
per-stage confidentiality of collection. Stage isolation holds at the write prefix and at the reader.

| What | Where in the ops bucket |
| --- | --- |
| A stage's pod logs | `activations/<stage>/logs/v2/namespace=<namespace>/date=<YYYY-MM-DD>/hour=<HH>/<stage>-<uuid>.ndjson.gz` |
| `olf-system` pod logs | `platform/logs/v2/namespace=olf-system/date=<YYYY-MM-DD>/hour=<HH>/platform-<uuid>.ndjson.gz` |
| Collector logs archived before hour partitions | `.../date=<YYYY-MM-DD>/<stage>-<uuid>.ndjson.gz`, read as before |
| Logs archived before the collector | `activations/<stage>/logs/k8s/...`, read exactly as before |

The date and hour are UTC and are those of every line in the object, taken from each line's own time, not from when
the object was uploaded. A read of a run's window or of one `--hour` lists only the hours it touches, and fetches
up to eight of the listed objects at once.

On AWS and Azure there is no collector yet: a `*/15` CronJob writes the older layout, and a Job pod deleted by its TTL
between two runs is not captured there.

The platform prefix is outside every stage reader's grant, so `olf logs` does not show it.

## Archive format

Format v2 ([ADR 0022](../../adr/0022-pod-log-collection-and-archive-format.md) D4) is gzipped NDJSON, one object per
upload, readable with `zcat`. The collector tags each record `<namespace>.<date>.<hour>` of the record's own UTC time,
and the S3 output keys the object from that tag, so an object uploaded late after an outage still sits under the hour
of its lines. The key holds no strftime time: Fluent Bit 5.2.0 takes that from the restart for a file left in the
spool. A collector upgraded with date-only tags still on disk writes `hour=<namespace>.<date>[2]/`, because the plugin
substitutes the whole tag for a missing part; the reader reads any such sub-prefix of a day whole.

```text
s3://openlakeforge-ops/activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/hour=09/dev-4f1b6c2e.ndjson.gz
s3://openlakeforge-ops/platform/logs/v2/namespace=olf-system/date=2026-11-16/hour=09/platform-0b7e5d11.ndjson.gz
```

| Field | Source | Notes |
| --- | --- | --- |
| `schema` | constant | `openlakeforge.k8s_log.v2`; the reader skips other values |
| `time` | the CRI timestamp, UTC, nanoseconds | The container runtime's clock, not the collector's |
| `stage` | the input | `dev`, `uat`, `prod` or `platform` |
| `namespace`, `pod`, `container` | the log file name | Always present |
| `container_id` | the log file name | Distinguishes a restarted container |
| `stream` | CRI | `stdout` or `stderr` |
| `component` | label `app.kubernetes.io/name` | Best effort |
| `domain`, `product` | labels `openlakeforge.io/domain`, `openlakeforge.io/product` | Best effort; no workload sets them yet |
| `run_id` | label `dagster/run-id`, else the `dagster-run-<id>` pod name | Best effort |
| `message` | the log line | A string, never merged into the record |
| `missing` | the record builder | The best-effort fields it could not fill, so absence is visible; an absent field is left out, not written as `null` |
| `dedup_key` | `<container_id>:<time>:<offset>` | `offset` from the tail input's `Offset_Key`; a replay of one line produces the same key |

```json
{"schema":"openlakeforge.k8s_log.v2","time":"2026-11-16T09:41:07.123456789Z","stage":"dev","namespace":"olf-dev","component":"floe","pod":"floe-sales-x7k2p","container":"runner","container_id":"3f9c0a41d5e2","stream":"stderr","run_id":"8f1c2d4e-0b7a-4c55-9a10-2f6e7a3b9c01","message":"2026-11-16 09:41:07 ERROR contract violated: orders.amount","missing":["domain","product"],"dedup_key":"3f9c0a41d5e2:2026-11-16T09:41:07.123456789Z:48213"}
```

The record is built by a Lua filter from the file name (`Path_Key`), the CRI timestamp and the labels the `kubernetes`
filter adds. It writes a fresh table, so nothing a workload prints can add or replace a field. The v1 layout, written
by the CronJob, is `<base>/k8s/namespace=<ns>/date=<D>/hour=<HH>/k8s-logs-<stamp>.ndjson.gz` with
`schema: openlakeforge.k8s_log.v1`, partitioned by when it was archived.

## Collector configuration

The configuration is rendered by `infra/terraform/modules/observability/log-collector` (`fluent-bit.conf.tftpl`).

| Setting | Value | Why |
| --- | --- | --- |
| Input | one `tail` per collected namespace, `Path /var/log/containers/*_<namespace>_*.log`, `multiline.parser cri`, its own `DB` file, `Read_from_Head On`, `Refresh_Interval 5`, `Skip_Long_Lines On`, `Buffer_Max_Size 1M` | The file name picks the route; a pod started while the collector was down is read from its first line |
| Buffering | `storage.type filesystem` on every input; engine chunks bounded by `storage.total_limit_size` (256M) | A collector restart or an object-store outage keeps the chunks |
| Metadata | `kubernetes` filter with `Merge_Log Off`, `Annotations Off`, `Use_Kubelet Off` | A workload's JSON stays a string in `message`; annotations can carry customer text; `nodes/proxy` is not requested |
| Re-tag | `rewrite_tag` to `<namespace>.<date>.<hour>`, `Emitter_Mem_Buf_Limit 20M`, service `Flush 1` | One flush interval of ingest fits the emitter, measured lossless at about 6 MB/s with the store reachable |
| Output | `out_s3` per stage, `use_put_object On`, `compression gzip`, `total_file_size 8M`, `upload_timeout 5m` | `PutObject` leaves no unfinished multipart uploads |
| Spool | `store_dir` per stage, `store_dir_limit_size` 512M (never `0`, which is unlimited) | The loss point of a long outage, sized and alerted on |
| Retries | `retry_limit` 1000000, `retry_exhausted_action quarantine`, `quarantine_dir_limit_size` 128M | The default of 5 retries moved a file out of the spool within about a minute, where nothing resends it; retrying until the spool limit makes that limit the loss point. A quarantined file leaves the spool budget, so it has its own |
| Connect timeout | `net.connect_timeout 2` | A store that drops packets holds the output for two connect timeouts per attempt; at the 10 s default the output took in almost nothing and records overflowed the emitter |
| State | `hostPath` `/var/lib/openlakeforge/log-collector` | A replaced pod on the same node keeps its offsets, chunks and spool |
| Telemetry | `HTTP_Server On`, `Health_Check On` | The engine's S3 output metrics are not used for delivery health ([below](#when-delivery-stops)) |

## Read logs

`olf logs` reads both layouts and returns each line once. Run it by run id, with no pod name:

```bash
olf logs --stage dev --run-id <dagster-run-id> --severity error
olf logs --stage dev --product <product> --component floe --since 2026-11-17T09:00:00
olf logs --stage dev --date 2026-11-17 --severity warning --grep "contract violated"
```

`--severity` keeps lines at that level or above, read from the line's own text, so a line that states no level is left
out. `--grep` keeps lines whose message contains the text, ignoring case; it is plain text, not a pattern, and does not
match the kubelet timestamp of an older-layout line. Both are applied by the platform API before `--limit`, so the
limit counts matching lines. In the API they are the `severity` and `q` parameters of
`GET /api/v1/stages/<stage>/logs` and `GET /api/v1/stages/<stage>/runs/<run-id>/logs`. On the run route a page stops at
the three UTC days one read covers, so a search with few matches can answer fewer lines than `limit`, or none, with
`next` set; follow `next` until it is null.

The platform API holds each log read to 20 seconds, counted from when one of its two log read workers takes it. A
whole day of a busy stage can take longer and is answered with an error that says so: read fewer hours with `--hour`,
or `--since` and `--until`. Those are the flags that narrow what is fetched; `--pod`, `--run-id`, `--severity` and the
other filters still read every object of the hours asked for. When both workers stay busy for 5 seconds the API
answers 503 with `Retry-After`, and `olf logs` asks once more after that wait.

The flags are in the [CLI reference](../../reference/cli.md). `data-engineer` reads `dev`, `platform-admin` reads every
stage, and a request for another stage is refused by the API. The newest lines may not be archived yet: the S3 output
buffers for five minutes (`upload_timeout`) before it writes an object. Records carry `component`, `product` and
`run_id` when the pod's labels name them, and a `missing` list naming the fields that were not available.

## Retention

`spec.observability.logs.archive.retention_days` (default 30) is applied to the v2 prefixes as a SeaweedFS filer path
TTL, set by a Job in the shared namespace each time the platform is applied. An object written under a v2 prefix expires
that many days after it was written. Four limits:

- The rule reaches only objects written after it is set. Changing `retention_days` does not shorten the life of
  objects already stored.
- Objects under `k8s/` (the layout written before the collector) have no rule and are never expired.
- SeaweedFS 4.48 accepts an S3 lifecycle rule and returns it, but creates no TTL from it, so the archive does not use one.
- A path TTL counts at most 255 of a unit. Above 255 days the rule is in whole weeks, months (30 days) or years (365
  days): exact where the days divide (365 is `1y`, 3650 is `10y`), otherwise rounded up to the nearest one. An object
  is then kept up to 6 days longer than asked up to 1785 days, and up to 29 days longer above that (256 is `37w`, 259
  days).

`olf infra deploy` reports `logs.archive.retention_supported: true` in the provider contract on the local provider;
on AWS and Azure it is `false` and the value is a request only.

## When delivery stops

The collector writes into a spool on the node and uploads from it. The engine's S3 `errors` and `dropped_records`
counters count failed upload attempts, not lost records: they rise during an object-store outage while the records are
still safely spooled, and no engine metric reports the data discarded at the spool limit. Archive health is read
from three delivery signals instead, which `olf health <stage>` reports from the collector pods and the bucket
([ADR 0022](../../adr/0022-pod-log-collection-and-archive-format.md) D2; thresholds and the spool probe in
[health signals](../../reference/health-signals.md#log-archive-delivery)):

| Signal | Reads as late when |
| --- | --- |
| `log-archive.spool-age` | the oldest spooled file is older than 10 minutes (`warning`) or 30 minutes (`critical`) |
| `log-archive.spool-size` | the spool holds 50% (`warning`) or 90% (`critical`) of its limit, 512M per stage |
| `log-archive.newest-object` | the newest archive object is older than 15 minutes (`warning`) or 60 minutes (`critical`) while pods are logging |

A stage whose pods log nothing reads like a stalled collector. The component names are explained in
[service health](service-health.md#log-archivespool-age-log-archivespool-size-and-log-archivenewest-object).

1. Check that a collector pod runs on every node and has not restarted:

   ```bash
   kubectl -n olf-system get pods -l app.kubernetes.io/name=log-collector -o wide
   kubectl -n olf-system logs -l app.kubernetes.io/name=log-collector -c fluent-bit --tail=100
   ```

2. Check that the object store answers: `olf health <stage>` reports `object-store.list`. If it fails, fix that first.
   The collector uploads again by itself; nothing needs restarting.
3. If the collector's egress to the object store is blocked, compare its policy with the flow table in
   [ADR 0017](../../adr/0017-local-network-policy-and-rbac.md):

   ```bash
   kubectl -n olf-system get networkpolicy olf-flows-log-collector -o yaml
   ```

4. Watch the signals fall back to `healthy` once the spool is empty. Lines spooled during the outage appear in the
   archive in their own date and hour partition, not the hour they were uploaded.

### What survives

| Event | Result |
| --- | --- |
| Collector pod restarted or killed | It resumes from the read offsets and chunks on the node's `/var/lib/openlakeforge/log-collector`. Lines are neither dropped nor repeated in `olf logs`: a replayed line has the same `dedup_key` and is returned once. |
| Pod deleted by its TTL after it finished | The collector had already read its log file; its output stays in the archive and `olf logs --run-id` finds it. |
| Outage longer than the spool allows | The S3 output discards data at `store_dir_limit_size`; the `spool-size` signal reaches `critical` first. Which lines are lost is recorded by the failure-injection e2e, not promised here. |
| Node replaced or its state directory lost | Offsets and spooled files are lost with it: lines not yet uploaded, and lines of pods that ended while no collector ran, cannot be recovered. |
| A line longer than 1 MB | Skipped. |
| A burst the collector cannot read before the kubelet rotates the file | Lost, with no indicator. The kubelet keeps 5 files of 10 MiB per container by default. |
| Store drops packets while ingest is fast | Each upload attempt holds the S3 output for two connect timeouts, so it takes in less than the stage ingests and the excess is lost at the re-tag emitter before the spool limit, with no error and no counter. Measured at 5000 lines/s of 1 KB: about 95% lost at the 10 s default connect timeout, 20% at 2 s. Accepted as residual loss. |
| Pod deleted while the collector was down | Its unread lines are gone with the kubelet's files. |
| Pod gone before the metadata lookup | Its labels are absent; `missing` names the fields. Routing does not depend on them. |

Files the S3 output gave up on after its retries are kept on the node under a quarantine limit of 128M per stage;
past that limit a further exhausted file is deleted.

## Check the archive end to end

```bash
uv run --project tools/olf --locked olf infra e2e run --env local --checks all
```

runs the drills that cover this page: a short-lived failing pod whose output is read after the pod is gone, a collector
killed mid-stream, retention in the object store, and the delivery signals through an outage. A further drill fails
a product run after its Floe step, deletes the run's Jobs, and reads the run's failure, quality checks, one report
and its whole log window back through the platform API; it logs one `RUN_DRILLDOWN_JSON` line of counts. The
operator journey drill then opens that run in the Portal and saves screenshots under
`.tmp/walkthrough-screenshots/<namespace>/`; it needs Playwright and Chromium installed beside `olf`. The outage
drills run after both, since they flush their spools into the hour the run's window reads.
