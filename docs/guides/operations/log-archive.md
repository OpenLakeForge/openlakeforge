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
| A stage's pod logs | `activations/<stage>/logs/v2/namespace=<namespace>/date=<YYYY-MM-DD>/<stage>-<uuid>.ndjson.gz` |
| `olf-system` pod logs | `platform/logs/v2/namespace=olf-system/date=<YYYY-MM-DD>/platform-<uuid>.ndjson.gz` |
| Logs archived before the collector | `activations/<stage>/logs/k8s/...`, read exactly as before |

On AWS and Azure there is no collector yet: a `*/15` CronJob writes the older layout, and a Job pod deleted by its TTL
between two runs is not captured there.

The platform prefix is outside every stage reader's grant, so `olf logs` does not show it.

## Read logs

`olf logs` reads both layouts and returns each line once. Run it by run id, with no pod name:

```bash
olf logs --stage dev --run-id <dagster-run-id> --severity error
olf logs --stage dev --product <product> --component floe --since 2026-11-17T09:00:00
```

The flags are in the [CLI reference](../../reference/cli.md). `data-engineer` reads `dev`, `platform-admin` reads every
stage, and a request for another stage is refused by the API. The newest lines may not be archived yet: the S3 output
buffers for five minutes (`upload_timeout`) before it writes an object. Records carry `component`, `product` and
`run_id` when the pod's labels name them, and a `missing` list naming the fields that were not available.

## Retention

`spec.observability.logs.archive.retention_days` (default 30) is applied to the v2 prefixes as a SeaweedFS filer path
TTL, set by a Job in the shared namespace each time the platform is applied. An object written under a v2 prefix expires
that many days after it was written. Three limits:

- The rule reaches only objects written after it is set. Changing `retention_days` does not shorten the life of
  objects already stored.
- Objects under `k8s/` (the layout written before the collector) have no rule and are never expired.
- SeaweedFS 4.48 accepts an S3 lifecycle rule and returns it, but creates no TTL from it, so the archive does not use one.

`olf infra deploy` reports `logs.archive.retention_supported: true` in the provider contract on the local provider;
on AWS and Azure it is `false` and the value is a request only.

## When delivery stops

The collector writes into a spool on the node and uploads from it, so an object store that cannot be reached does not
show in the engine's own error counters. `olf health <stage>` reports three signals instead, read from the collector pods
and the bucket:

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
   archive in their own date partition, not the day they were uploaded.

### What survives

| Event | Result |
| --- | --- |
| Collector pod restarted or killed | It resumes from the read offsets and chunks on the node's `/var/lib/openlakeforge/log-collector`. Lines are neither dropped nor repeated in `olf logs`: a replayed line has the same `dedup_key` and is returned once. |
| Pod deleted by its TTL after it finished | The collector had already read its log file; its output stays in the archive and `olf logs --run-id` finds it. |
| Outage longer than the spool allows | The S3 output discards data at `store_dir_limit_size`; the `spool-size` signal reaches `critical` first. Which lines are lost is recorded by the failure-injection e2e, not promised here. |
| Node replaced or its state directory lost | Offsets and spooled files are lost with it: lines not yet uploaded, and lines of pods that ended while no collector ran, cannot be recovered. |
| A line longer than 1 MB | Skipped. |
| A burst the collector cannot read before the kubelet rotates the file | Lost, with no indicator. The kubelet keeps 5 files of 10 MiB per container by default. |

Files the S3 output gave up on after its retries are kept on the node under a quarantine limit of 128M per stage;
past that limit a further exhausted file is deleted.

## Check the archive end to end

```bash
uv run --project tools/olf --locked olf infra e2e run --env local --checks all
```

runs the drills that cover this page: a short-lived failing pod whose output is read after the pod is gone, a collector
killed mid-stream, retention in the object store, and the delivery signals through an outage.
