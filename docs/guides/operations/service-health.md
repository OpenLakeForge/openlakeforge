# Service health

`olf health <stage>` and `GET /api/v1/stages/{stage}/health` report the platform's own services next to its
pipelines. Each service row carries a state, the time the API observed it and the time that answer stops being usable
(`observed` and `valid until` in the table; `observed_at` and `expires_at` in `--json`). A probe that could not look
(timeout, refused permission, unreachable service) reads `unknown`, never `healthy`; a component the deployment leaves
out reads `disabled`. How the states are computed is in [health signals](../../reference/health-signals.md).

The table groups the rows by service, under a heading such as `Log archive: warning` that carries the worst state of
the service's components. Each row shows its `component` key and its display `name`; `last write` is the component's
own last durable write (`last_success_at` in `--json`), reported only for `log-archive.newest-object` and
`quality-sync.publication` and printed as `—` everywhere else, never as the probe time. An API that reports no service
for its rows lists them under `(service not reported)`.

```text
Log archive: warning
        component         |         name        |  state  |       observed       |     valid until      |      last write      |          reason
--------------------------+---------------------+---------+----------------------+----------------------+----------------------+------------------------
log-archive.spool-age     | Log spool age       | healthy | 2026-11-19T09:00:00Z | 2026-11-19T09:00:30Z | —                    | oldest spool 40s
log-archive.newest-object | Newest archived log | warning | 2026-11-19T09:00:00Z | 2026-11-19T09:00:30Z | 2026-11-19T07:02:11Z | newest object is 2h old
(2 rows)
```

The probes need no Prometheus, Loki or Grafana, so a Slim stack shows the same table. Without a metrics source the
stage lists the `metrics` integration as unavailable, and the service rows are unaffected.

## What to do for each component

The headings are the component names `olf health` prints, so a runbook link can point at the anchor.

### dagster.workloads

The Dagster Deployments are not ready, one is scaled to zero (the daemon, for example), cannot be scheduled, crash loop or restart repeatedly. In the stage namespace:

```bash
kubectl -n <stage-namespace> get pods -l app.kubernetes.io/name=dagster
kubectl -n <stage-namespace> describe pod <pod>
```

`unschedulable` points at node capacity or a taint; a crash loop at the pod's log.

### dagster.read-api

The webserver did not answer GraphQL, reports an error loading its repositories, or has no code location loaded.
Check `dagster.workloads` first. A loaded stack with no code location needs `olf infra deploy --phase artifacts`.

### trino.workloads and trino.readiness

The Trino pods are unhealthy, or the coordinator did not answer `/v1/info` or is still starting. Readiness does not
prove workers, catalogs or Polaris access: a green row is not a passing query.

### polaris.readiness

The Polaris catalog did not answer its readiness check (`/q/health/ready` on management port 8182) or reported `DOWN`.
The check is unauthenticated, so a green row proves only that the service is up, not that catalog reads or grants
work. Polaris runs in the platform namespace:

```bash
kubectl -n olf-system get pods -l app.kubernetes.io/name=polaris
kubectl -n olf-system logs <pod>
```

A pod that is not ready or restarts points at its log; a running pod that answers `DOWN` names the failing check in
the readiness response body.

### object-store.list

The stage's bucket could not be listed: the endpoint is unreachable, the key is refused, or the bucket is missing. It
does not prove a write is durable.

### log-archive.spool-age, log-archive.spool-size and log-archive.newest-object

Archive delivery has stopped or is late (ADR 0022). `spool-age` and `spool-size` come from the collector pods;
`newest-object` lists the archive and covers a collector that is down. A stage that logs nothing reads like a stalled
one. Check that the collector pods run and that `object-store.list` is not failing. Logs already archived stay
readable with `olf logs` while delivery is down.

### quality-sync.publication

Quality results are not reaching OpenMetadata in time (ADR 0007). The row reads the summary the stage's
`quality-reconciler-<stage>` CronJob writes after each pass; it says nothing about whether the data passed its checks,
which the pipelines' `quality` signal still shows. A stage without governance, as on Slim, reads `disabled` and lists
the `quality-sync` integration as `not_provided`. The thresholds are in
[health signals](../../reference/health-signals.md#publication-health).

| Reason starts with | What it means |
| --- | --- |
| `no publication pass for` | No pass has finished in 15 minutes or more: the CronJob is suspended, its pods fail, or its key cannot write |
| `... result(s) waiting, the oldest for` | Passes run but results stay unpublished: OpenMetadata refuses or is down, or a table is not registered there |
| `... result(s) gave up after 10 attempts` | Retries are exhausted; nothing publishes them until a replay |
| `... manifest(s) cannot be published` | A manifest is malformed, of an unsupported version, or names another stage, product or run |
| `no publication summary` | No pass has ever finished on this stage |

1. Look at the CronJob and its latest Job:

   ```bash
   kubectl -n olf-system get cronjob quality-reconciler-<stage>
   kubectl -n olf-system get jobs -l openlakeforge.io/job=quality-reconciler,openlakeforge.io/stage=<stage>
   kubectl -n olf-system logs job/<latest-job>
   ```

   A pod in `CreateContainerConfigError` means `olf infra openmetadata bootstrap` has not written the ingestion bot's
   Secret yet. A Job that exits 1 lost OpenMetadata or the bucket mid-pass.
2. Run a pass now instead of waiting for the schedule:

   ```bash
   kubectl -n olf-system create job --from=cronjob/quality-reconciler-<stage> quality-reconciler-<stage>-now
   ```

3. When results gave up, give them a fresh set of attempts once OpenMetadata answers:

   ```bash
   uv run --project tools/olf olf infra openmetadata quality-sync --stage <stage> --retry-failed
   ```

4. A table that is not registered keeps its results waiting; the publisher never creates one. Once OpenMetadata's
   catalog ingestion has registered the table, the next pass publishes them.

While the row is degraded the pipelines run as before and the Portal's quality status is unaffected; only
OpenMetadata's test history is behind.

### Metrics scrape staleness

Not reported yet. There is no metrics source connected to the API (#21), so the stage lists the `metrics` integration
as `not_provided` rather than showing a failed signal.

## The limit of in-app monitoring

The health routes are served by the platform API. If the API is down, no health answer exists, and the API cannot
report its own outage or alert about it. The evaluator in `olf-system` shares the cluster, so it cannot either (see
[alerts](alerts.md#the-limit-of-an-in-cluster-evaluator)).

A minimal outside-in check, run from the operator's workstation or any monitor outside the cluster:

```bash
olf health <stage>; echo "exit $?"      # 1 when the API cannot be read, or the stage is critical or unknown
curl -fsS https://<platform-api-host>/api/healthz
```

Treat a non-zero exit, or a `healthz` that does not answer, as the platform being down until `kubectl -n olf-system
get pods` says otherwise. Polling `alerts/state/v1/state.json` and its `evaluated_at` covers the evaluator.
