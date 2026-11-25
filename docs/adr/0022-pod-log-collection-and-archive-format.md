# ADR 0022: Pod log collection and the log archive format

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ (#22) · AWS ✘ (polling CronJob, v1 only) · Azure ✘ (polling CronJob, v1 only) |

## Context

Pod logs first reached the ops bucket through a `*/15` CronJob per stage
namespace that read the last hour of every pod still present. A pod deleted
between two runs was never read, and Floe Jobs and Dagster run Jobs are
deleted an hour after they finish; a line that was read was written up to
four times. [#22](https://github.com/OpenLakeForge/openlakeforge/issues/22)
set the direction: a lightweight collector writes durable archives to the ops
object store, Fluent Bit is the S3-compatible candidate, Alloy qualifies only
if it meets the same guarantees, and no Loki is required.
`spec.observability.logs.archive` is always on and carries `retention_days`;
indexed search (`logs.search`) stays optional and separate. A collector on the
node reads every namespace's log files, so stage isolation has to hold
somewhere other than collection. Archives already written by the CronJob must
stay readable while it runs and after it goes.

## Decision

**D1. A Fluent Bit DaemonSet is the pod log path where it runs; elsewhere the
polling CronJob stays.** On local, in Slim and Full alike, a Fluent Bit 5.2.0
DaemonSet in `olf-system` tails the kubelet's container log files and writes
the archive to the ops bucket, and the polling CronJob is not rendered there,
because a second capture would write every line twice. AWS and Azure have no
collector and keep the CronJob, which writes the v1 layout, as their only
capture. There is no Loki; an indexed backend is the optional #795. The image
is pinned by digest as `fluent_bit` in `release/component-catalog.yaml`.
Enforced by: `infra/terraform/modules/observability/log-collector`, the
`node_collector` switch in `tools/olf/olf/infra/deployment/activation.py`
(`tools/olf/tests/infra/test_deployment_activation.py`), `olf check components`.

**D2. The spool limit is the loss point, and delivery is judged from the spool
and the bucket, never from the engine's output metrics.** Each stage's S3
output retries an upload until its spool (`store_dir`) reaches an explicit
`store_dir_limit_size`, never `0`, which is unlimited; an outage longer than
the spool lasts loses data there, at a size an operator sets for an outage
budget and is warned of before it is reached. The `rewrite_tag` emitter is a
second, silent loss point, reached before the spool limit when the store
drops packets while ingest is fast; `Flush 1`, `Emitter_Mem_Buf_Limit 20M` and
`net.connect_timeout 2` bound it and the residual loss is accepted. The
engine's S3 `errors` and `dropped_records` counters rise during an outage
while the records are still spooled, so delivery health is three signals
instead: the age of the oldest spooled file and the spool's size, read from
the collector pods by the `spool-probe` sidecar, and the newest archive
object, listed with the log reader's key. Their thresholds are those the
maintainer accepted on 2026-11-18, and a signal that cannot be read is
`unknown`, never `healthy`. Enforced by:
`infra/terraform/modules/observability/log-collector` (`fluent-bit.conf.tftpl`,
the `spool_limit_size` validation), `ArchiveThresholds` in
`packages/core/openlakeforge_core/archive_health.py`,
`tools/olf/olf/infra/e2e/_log_delivery.py`.

**D3. Routing is by the namespace in the log file name, never by pod metadata,
and collection makes no per-stage confidentiality claim.** Each collected
namespace has its own `tail` input whose path glob names the namespace segment
the kubelet writes into the file name, and its own S3 output to its stage's
prefix. Nothing in a record, label or annotation changes the route: the
`kubernetes` filter runs after routing and only adds fields, `stage` is written
from the input's constant, and `Merge_Log Off` keeps a workload's JSON out of
the record. A namespace in no input is not collected. A node collector can read
every stage's logs on its node, so isolation between stages holds at the write
prefix and at the reader only: the collector's one key may `Write` each v2
prefix and nothing else (no `Read`, no `List`), and the platform API's role
matrix decides who reads a stage's prefix, with the stage as the path segment
it checks. The `spool-probe` sidecar runs as uid 0, read-only, with no S3 key,
inside the collector's boundary. `olf-system` is collected into
`<ops>/platform/logs/v2/`, under no stage. Its services serve every stage, so
one of its logs can hold any stage's activity, PROD included: the platform
API's log reader key may read the prefix, and its matrix lets only
`platform-admin` do so. Nothing writes v1 there, so the grant covers `v2/`
alone and a platform read lists no v1 prefix. Enforced by:
`fluent-bit.conf.tftpl`, the one-route-per-namespace validation in
`log-collector/variables.tf`, `log-collector/tests/collector.tftest.hcl`, the
`openlakeforge-log-collector` identity in
`infra/terraform/modules/storage/seaweedfs/credentials.tf`, the `list_logs`
and `list_platform_logs` rows of
`openlakeforge_platform_api/permissions.toml` and `log_reader_prefixes` in
`infra/terraform/environments/local/main.tf`.

**D4. Archive format v2 is gzipped NDJSON, partitioned by the records' own UTC
date and hour.** An object is
`<base>/v2/namespace=<ns>/date=<YYYY-MM-DD>/hour=<HH>/<stage>-<uuid>.ndjson.gz`,
where `<base>` is the stage's `s3://<ops>/activations/<stage>/logs` (or
`s3://<ops>/platform/logs`), so the stage is the leading path segment. Every
record in an object has the date and hour its key names, however late it is
uploaded: the collector tags each record `<namespace>.<date>.<hour>` of its
own time and the key is built from the tag, never from a strftime time,
which Fluent Bit takes from the restart for a file left in the spool. A record
carries `schema: openlakeforge.k8s_log.v2`, the CRI time, the stage, the
namespace, pod, container and container id from the file name, the line as a
string in `message` (never merged), best-effort label fields with `missing`
naming each one absent, and a `dedup_key` that a replay of the same line
reproduces. The fields are listed in the
[log archive guide](../guides/operations/log-archive.md#archive-format).
Enforced by: the record builder and `s3_key_format` in
`infra/terraform/modules/observability/log-collector`,
`tools/olf/olf/infra/e2e/_log_collector.py`.

**D5. One reader, `LogArchive`, reads v2 and v1; v1 stays readable.** v1
objects (`<base>/k8s/...`) are never rewritten, and the reader keeps reading
them for as long as any can exist. Both layouts are listed in one call and
answer in one response shape, `ArchivedLogLine`, whose v2-only fields are
additive and optional. Within v2 a record whose `dedup_key` was already
returned is dropped. Across layouts a v1 line is dropped only when v2 holds a
line with the same namespace, pod, container, timestamp and text; every other
v1 line is kept, with `source` set on each line so a duplicate is visible,
never a loss. Authorization is unchanged by the layout: the stage is the path
segment the permission matrix checks. Enforced by:
`packages/lakehouse-services/openlakeforge_services/logs.py`,
`tools/olf/tests/common/test_log_archive_v2.py`.

**D6. Retention is applied by the object store; on local it is a SeaweedFS
filer path TTL on each v2 prefix.** `retention_days` is applied to the v2
prefixes and reported through the ops bucket contract
(`retention_supported`). On local a Job sets `fs.configure -ttl` on each prefix,
not an S3 lifecycle rule, which SeaweedFS stores without creating a TTL from
it. A path TTL counts 1 to 255 of a unit, so up to 255 days the TTL is in
days and above that it is the shortest whole number of weeks, months (30 days)
or years (365 days) that is at least `retention_days`, the larger unit on a
tie: rounded up, never down. The rule reaches only objects written after it is
set. AWS and Azure apply none (`retention_supported: false`). Enforced by:
the `object_retention` Job of `infra/terraform/modules/storage/seaweedfs`,
`seaweedfs/tests/object_retention.tftest.hcl`,
`tools/olf/olf/infra/e2e/_log_retention.py`.

**D7. A read of the archive is bounded by what it lists, fetches and holds,
and returns exactly what a sequential read would.** A v2 read lists only the
hour partitions its window touches, with no slack hour, and reads a day's
objects that sit outside an hour partition whole. With a window start, an
object whose `LastModified` is more than `UPLOAD_CLOCK_SLACK` (15 minutes)
before it is skipped unread; no upper bound is taken, because a spool uploads
old lines late. v1 is listed `V1_LOOKBACK_SECONDS` past the window's end,
because it is partitioned by archive time. Up to `FETCH_AHEAD` (8) objects are
fetched at once on one process-wide pool, while decoding, filtering,
ordering, dedup, `limit` and the cursor stay on the reading thread in key
order. Each object is decompressed a line at a time, and only `\n` ends a
record. The read checks its deadline and an optional `cancelled` callable
between listing pages, objects and records and before each queued GET; a
cancelled read raises `ArchiveCancelled` and answers nothing. How the HTTP
routes bound a read is
[ADR 0021](0021-platform-api.md) D9's. Enforced by:
`packages/lakehouse-services/openlakeforge_services/logs.py`,
`tools/olf/tests/common/test_log_archive_fetch.py`,
`tools/olf/tests/common/test_log_archive_v2.py`.

The collector's configuration, the failure modes and the record fields are in
the [log archive guide](../guides/operations/log-archive.md); the delivery
signals, their thresholds and the spool probe are in
[health signals](../reference/health-signals.md#log-archive-delivery).

## Consequences

- A compromised collector pod reads every stage's logs on its node (D3).
- The collector's key is one key for all stages, so its scope cannot stop a
  misconfigured output from writing to another stage's prefix, and SeaweedFS
  gates overwrite and delete on `Write`: the archive is not append-only.
- Loss without an indicator remains in three places: the emitter under an
  outage that drops packets (D2), a burst the kubelet rotates away before the
  collector reads it, and the lines of a pod that ended while no collector ran.
  A node replaced with its state directory loses what it had not uploaded.
- Which data the S3 plugin discards at the spool limit is recorded by the
  failure-injection e2e, not promised.
- A stage whose pods log nothing reads like a stalled collector on the newest
  object signal. That signal is not computed for the platform prefix, so
  `olf-system` has the spool signals only.
- The API's one log reader key covers every stage's archive and the platform's,
  so its permission matrix is all that separates their readers.
- No workload sets `openlakeforge.io/domain` or `openlakeforge.io/product`
  yet, so those record fields stay in `missing`.
- Per-node CPU and memory are not claimed: the footprint drill
  (`_log_footprint.py`) measures them per cluster.
- On AWS and Azure a Job pod deleted between two CronJob runs is lost and
  overlapping windows repeat lines, and no retention is applied
  ([technical debt](../technical-debt.md)).
- A path TTL can keep an object up to 29 days longer than asked, never
  shortens the life of objects already stored, and v1 objects on local have
  no rule and never expire.

## Boundaries

| Not decided here | Where |
| --- | --- |
| The log routes, their role floor and how they bound a read | [ADR 0021](0021-platform-api.md) D3, D9 |
| The collector's and the probe's network flows (F37, F42) | [ADR 0017](0017-local-network-policy-and-rbac.md), `release/network-flows.yaml` |
| Indexed log search (`logs.search`) | #795 |
| Alerting on the delivery signals | [ADR 0023](0023-alert-evaluation-and-webhook-delivery.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Alloy with `otelcol.exporter.awss3` | Its S3 exporter is experimental and writes no line-per-record format; its image is about four times Fluent Bit's on a DaemonSet that runs on every node |
| One DaemonSet per stage | The only way to give each stage its own write key, at the cost of a collector per stage per node; the key would still not limit what the node's collector can read |
| Routing by pod label | A label is set by whoever creates the pod |
| Keep polling, shorten the interval | Short-lived pods still vanish between runs and the overlap stays |
| A retry count as the loss point | It discards data on the first long outage; a size limit can be sized and alerted on |
| A strftime time in the object key | Fluent Bit takes it from the restart for a file left in the spool, so after an outage it names the wrong hour |
| An S3 lifecycle rule for retention on local | SeaweedFS stores it and never creates a TTL from it |
| Loki | Not required by #22; an indexed backend is the optional #795 |

## History

- 2026-11-16 (#1120): New record, decisions 1 to 6 (now D1 to D6).
- 2026-11-16 (#1189): D2 gained the three delivery signals and the spool
  probe; D3 gained the probe's place inside the collector's boundary.
- 2026-11-17 (#22): D1 changed: the polling CronJob is not rendered where the
  collector runs; AWS and Azure keep it.
- 2026-11-18 (#1123): D2 changed: the engine's S3 counters rise without loss,
  and the `rewrite_tag` emitter is named a second loss point, bounded by
  `Flush 1`, `Emitter_Mem_Buf_Limit 20M` and `net.connect_timeout 2`.
- 2026-11-18: D2 and D3: the maintainer accepted the delivery-signal
  thresholds and the probe's uid 0.
- 2026-11-18 (#1248): D6 changed from an S3 lifecycle rule to a SeaweedFS
  filer path TTL, rounded up above 255 days.
- 2026-11-20 (#1326): D4 changed: objects are partitioned by the records' own
  UTC hour, not by day only.
- 2026-11-23: Rewritten in the standard form. Decisions 1 to 6 keep their
  numbers as D1 to D6; the image pin moved from decision 6 into D1; the
  reader's listing, fetch, decode and cancellation rules (#1326, #1347 and
  the 2026-11-20 and 2026-11-21 reader changes) became D7. The collector
  configuration, failure modes and record fields moved to the log archive
  guide, the delivery signals and the probe to health signals. No decision
  changed.
- 2026-11-25: D3 gained the platform prefix's reader, until then undecided: the
  platform API, for `platform-admin` only, as the maintainer chose.
