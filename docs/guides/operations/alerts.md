# Send alerts to a webhook

Configure a webhook receiver so OpenLakeForge posts a notification when a
pipeline run fails, a scheduled run never starts, output goes stale or a
quality check fails. It covers the receiver and its Secret, what a
notification contains, and what to do for each condition.

An alert evaluator runs every 5 minutes as a CronJob in `olf-system`, on the
local provider. It reads each enabled stage's Dagster and its quality
manifests, keeps its state in the ops bucket under `alerts/`, and posts to your
receivers. It needs no Prometheus, Loki or Grafana, and it exists only when
the profile declares at least one receiver. The design is
[ADR 0023](../../adr/0023-alert-evaluation-and-webhook-delivery.md).

## Configure a receiver

1. Create the Secret in `olf-system` before you deploy. The webhook URL is the
   receiver's credential, so `olf` never creates it and Terraform never holds
   its value:

   ```bash
   kubectl -n olf-system create secret generic alert-webhooks \
     --from-literal=webhook-url=<the webhook URL>
   ```

2. Declare the receiver in `openlakeforge.yaml`:

   ```yaml
   spec:
     observability:
       alerts:
         receivers:
           - name: on-call
             type: webhook
             stages: [prod]
             secret_ref:
               name: alert-webhooks
               key: webhook-url
   ```

3. Validate and deploy:

   ```bash
   olf profile validate --project .
   olf infra platform apply -f openlakeforge.yaml
   ```

The deploy fails, naming the receiver, when the Secret or the key is missing.
To change the URL, replace the Secret; the next evaluation reads it.

| Field | Meaning |
| --- | --- |
| `name` | Names the receiver in logs and history. The URL never appears there. |
| `type` | `webhook` only: an HTTPS endpoint that accepts a JSON `POST`. |
| `secret_ref` | The Secret and key that hold the URL. |
| `stages` | Optional. The stages this receiver hears about, for example `[prod]` to keep DEV quiet. Omitted means every enabled stage. |

The field reference is in the
[deployment profile](../../reference/deployment-profile.md#specobservability).

Delivery rules:

- The URL must be `https`. A redirect is a rejected delivery and is not
  followed.
- Each notification is retried up to 3 times in one evaluation, then left
  queued for the next evaluations, and given up after 12.
- Delivery is at-least-once. Every attempt of one notification carries the same
  `Idempotency-Key: <incident_id>:<event>` header, so a receiver can drop
  repeats.
- There are no reminders and no acknowledgement. An incident notifies once
  when it opens and once when it resolves; a warning that becomes critical
  sends one `escalated`.

## A notification

```json
{
  "schema": "openlakeforge.alert.v1",
  "event": "opened",
  "incident_id": "3f2a9c-1763294400-1",
  "condition": "run_failed",
  "severity": "critical",
  "stage": "prod",
  "product": "order_revenue_pipeline",
  "run_id": "8f1c2d3e-0000-4000-8000-000000000000",
  "opened_at": "2026-11-16T12:05:00+00:00",
  "event_at": "2026-11-16T12:05:00+00:00",
  "summary": "latest run failed",
  "links": {
    "run": "https://dagster-prod.olf.example.com/runs/8f1c2d3e-0000-4000-8000-000000000000"
  }
}
```

| Field | Content |
| --- | --- |
| `event` | `opened`, `escalated` or `resolved`. |
| `condition` | One of the conditions below. |
| `severity` | `warning` or `critical`. |
| `stage`, `domain`, `product`, `table` | Where it happened; absent fields are left out. |
| `run_id` | The Dagster run, when the condition has one. |
| `summary` | A fixed phrase, never log or error text. |
| `links.run` | The run in the stage's Dagster, behind the perimeter: the reader signs in and needs a role for that stage. |
| `links.runbook` | The anchor for the condition on a runbook page. Omitted unless the evaluator has a runbook base URL, which no profile field sets today. |

A payload never carries log lines, run tags, error text, row contents, storage
URLs or credentials.

## What to do for each condition

The headings are the `condition` values, which is the anchor a runbook link
ends with.

### run_failed

Critical. The latest run of a job failed. A canceled run does not alert.
Open `links.run`, read the failed step, and fix or rerun. It resolves when the
latest run succeeds; a run still in progress does not resolve it. See
[troubleshooting](../troubleshooting.md).

### missed_run

Critical. A running schedule passed its due time plus 15 minutes and no run
started, which usually means the Dagster scheduler is down. Check the
scheduler daemon in the stage's Dagster. It resolves when a run starts. If you
stop the schedule while it is open, it closes without a notification.

### stale_output

Critical. A running schedule's job has had no success since the previous due
time plus the 15 minute grace. It is not sent while `run_failed` or
`missed_run` is open for the same product, since either already explains it.
It resolves on the next success. The signal is described in
[health signals](../../reference/health-signals.md).

### quality

`warning` for a Floe warning or a non-blocking error, `critical` for a blocking
error. It is one incident whose severity can rise, so a warning that becomes a
blocking error sends one `escalated`. It resolves when a later run's complete
quality manifest passes. Thresholds stay in your Floe and dbt configuration.

### schedule_mismatch

Warning. A PROD schedule that the product's cadence declares running is
stopped in Dagster. Start it, or change the declaration. It closes without a
notification when either changes.

### stage_unreadable

Critical. Dagster for a stage could not be read in 3 consecutive evaluations.
Check that the stage's Dagster webserver is running. While it lasts, that
stage's open incidents are held, not resolved.

## Check it

The health routes do not depend on receivers: with none declared,
`GET /api/v1/stages/{stage}/health` still reports a failed run as `critical`.

Failed evaluations stay visible as Jobs, and each run logs one summary line:

```bash
kubectl -n olf-system get jobs -l openlakeforge.io/job=alert-evaluator
kubectl -n olf-system logs job/<job name>
```

## The limit of an in-cluster evaluator

The evaluator runs inside the cluster it watches. A whole-cluster outage, a
node pool that cannot schedule `olf-system`, a stopped CronJob controller or an
unreachable object store produces no alert. If it cannot read or write its
state it delivers nothing and the Job fails. To catch those cases, watch from
outside: the state object `alerts/state/v1/state.json` carries `evaluated_at`,
the time of the last evaluation, so a monitor that polls it can tell a stopped
evaluator from a quiet one.
