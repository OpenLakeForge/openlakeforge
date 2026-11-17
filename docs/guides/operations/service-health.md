# Service health

`olf health <stage>` and `GET /api/v1/stages/{stage}/health` report the platform's own services next to its
pipelines. Each service row carries a state, the time the API observed it and the time that answer stops being usable
(`observed` and `valid until` in the table; `observed_at` and `expires_at` in `--json`). A probe that could not look
(timeout, refused permission, unreachable service) reads `unknown`, never `healthy`; a component the deployment leaves
out reads `disabled`. How the states are computed is in [health signals](../../reference/health-signals.md).

The probes need no Prometheus, Loki or Grafana, so a Slim stack shows the same table. Without a metrics source the
stage lists the `metrics` integration as unavailable, and the service rows are unaffected.

## What to do for each component

The headings are the component names `olf health` prints, so a runbook link can point at the anchor.

### dagster.workloads

The Dagster Deployments are not ready, cannot be scheduled, crash loop or restart repeatedly. In the stage namespace:

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

### object-store.list

The stage's bucket could not be listed: the endpoint is unreachable, the key is refused, or the bucket is missing. It
does not prove a write is durable.

### log-archive.spool-age, log-archive.spool-size and log-archive.newest-object

Archive delivery has stopped or is late (ADR 0022). `spool-age` and `spool-size` come from the collector pods;
`newest-object` lists the archive and covers a collector that is down. A stage that logs nothing reads like a stalled
one. Check that the collector pods run and that `object-store.list` is not failing. Logs already archived stay
readable with `olf logs` while delivery is down.

### Metrics scrape staleness and quality publisher lag

Not reported yet. There is no metrics source connected to the API (#21) and no quality publisher (#797), so neither
is probed; the stage does not show a publisher that does not exist as failed.

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
