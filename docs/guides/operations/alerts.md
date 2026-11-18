# Send alerts to a webhook

Configure a webhook receiver so OpenLakeForge posts a notification when a
pipeline run fails, a scheduled run never starts, output goes stale or a
quality check fails. It covers the receiver and its Secret, the alert policy
that says which products alert and who is told, what a notification contains,
and what to do for each condition.

An alert evaluator runs every 5 minutes as a CronJob in `olf-system`, on the
local provider. It reads each enabled stage's Dagster and its quality
manifests, keeps its state in the ops bucket under `alerts/`, and posts to your
receivers. It needs no Prometheus, Loki or Grafana, and it exists only when
the profile declares at least one receiver. The design is
[ADR 0023](../../adr/0023-alert-evaluation-and-webhook-delivery.md).

> [!WARNING]
> **Product alerts are silent unless `lakehouse_code/alerts.yml` declares
> them.** A project without that file gets only the platform alerts
> ([`stage_unreadable`](#stage_unreadable), [`policy_unreadable`](#policy_unreadable),
> [`service_degraded`](#service_degraded) and
> [`health_unreadable`](#health_unreadable)). Before the policy existed, every
> product's conditions alerted. If you deployed receivers from such a build,
> upgrade like this:
>
> 1. Add `lakehouse_code/alerts.yml`. A project made by `olf init` carries the
>    sample one; an existing project, and one made with `olf init --empty`,
>    does not. [Write the policy](#write-the-alert-policy) or copy the sample's.
> 2. Run `olf check contracts`, then `olf alert explain` for a product (or a
>    job) and a stage to see what will alert.
> 3. Build a revision and deploy it to each stage
>    ([promote DEV to PROD](../promote-dev-to-prod.md)). The evaluator reads the
>    policy of the stage's active revision, and activation fails when a route
>    names a receiver that `openlakeforge.yaml` does not declare.
>
> The first evaluation after the upgrade resolves, without a notification,
> every firing product incident that no rule covers.

## Configure a receiver

1. Create the Secret in `olf-system` before you deploy. The webhook URL is the
   receiver's credential, so `olf` never creates it and Terraform never holds
   its value:

   ```bash
   WEBHOOK_URL='https://hooks.example.com/services/...'
   kubectl -n olf-system create secret generic alert-webhooks \
     --from-literal=webhook-url="$WEBHOOK_URL"
   ```

   Keep the URL in single quotes: webhook URLs often contain `&`, which the
   shell would otherwise treat as a command separator.

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
| `stages` | Optional. The stages this receiver hears about, for example `[prod]` to keep DEV quiet. Omitted means every enabled stage. It is a ceiling: a rule in `alerts.yml` cannot send a stage's alerts to a receiver that leaves that stage out. |

The field reference is in the
[deployment profile](../../reference/deployment-profile.md#specobservability).

### Platform receivers

Platform conditions are not product alerts, so `alerts.yml` cannot route them.
The profile does, with `platform_receivers`:

```yaml
spec:
  observability:
    alerts:
      receivers:
        - name: on-call
          type: webhook
          secret_ref: {name: alert-webhooks, key: webhook-url}
        - name: sales-webhook
          type: webhook
          secret_ref: {name: alert-webhooks, key: sales-url}
      platform_receivers: [on-call]
```

Omitted, every receiver gets the platform conditions. A list that is present
must be non-empty, name declared receivers and repeat none; an empty list is a
validation error, because it would silence every platform condition. Omit the
field to say "all". A receiver's `stages` still applies, so a `prod`-only
receiver never hears about `dev`. `olf profile resolve --project . --json`
shows the effective list as `observability.alerts.platform_receivers`.

Delivery rules:

- The URL must be `https`. A redirect is a rejected delivery and is not
  followed.
- A connection failure, a timeout, `408`, `429` or a `5xx` response is
  retried up to 3 times in one evaluation, then left queued for the next
  evaluations, and given up after 12.
- Any other `4xx` response (for example `400`, `401`, `403` or `404`) is a
  rejection: it is recorded and never resent. Fixing the receiver afterwards
  does not recover that notification, and with no reminders the incident's
  next notification is its `resolved` or `escalated`.
- Delivery is at-least-once. Every attempt of one notification carries the same
  `Idempotency-Key: <incident_id>:<event>` header, so a receiver can drop
  repeats.
- There are no reminders and no acknowledgement. An incident notifies once
  when it opens and once when it resolves; a warning that becomes critical
  sends one `escalated`.

`GET /api/v1/stages/{stage}/alerts/incidents` on the platform API lists a
stage's open incidents, read-only, for `data-engineer` and above. Each carries
its latest notification's `delivery`:

| `status` | Meaning |
| --- | --- |
| `pending` | Queued: not every receiver routed to it has it yet |
| `delivered` | Every receiver routed to it accepted it |
| `undeliverable` | It was given up after 12 evaluations, or a receiver rejected it |
| `unrouted` | No receiver was routed to it, so nobody was told |
| `unknown` | It left the queue before the evaluator kept outcomes |

The answer's `state` is `not_provided` without an evaluator or the API's key
to read its state, and
`unavailable` with `incidents: null` when the state cannot be read or the
evaluator has not written one yet. `stale: true` means the last evaluation is
more than 10 minutes old, so the list may be out of date. Receiver names and
URLs never appear in the answer.

## Write the alert policy

`lakehouse_code/alerts.yml` says which conditions alert for which products and
stages, with which thresholds, and which receivers hear about each. It is
optional and silent by default: a product, stage or condition that no rule
names does not alert. The profile still owns the receivers and their Secrets; a
policy only names them. Every key, type and default is in the
[alert policy reference](../../reference/alert-policy.md).

The file travels in the project revision, so each stage is judged by the policy
of its active revision and a change reaches a stage when you deploy a revision
to it. `olf check contracts` validates it offline, including that every
`match` names a domain and product of `governance.yml`.

```yaml
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy

routes:
  on-call: on-call # route name: receiver name in openlakeforge.yaml
  sales: sales-webhook

defaults:
  stages: [prod]
  notify: [on-call]

rules:
  # Every job in PROD, maintenance jobs included.
  - match: {domain: "*", product: "*"}
    when:
      run_failed: {}
      missed_run: {}
      freshness: {}
      schedule_mismatch: {}
      quality: {level: error}

  # The sales domain hears its own quality warnings; the pager does not.
  - match: {domain: sales, product: "*"}
    notify: [sales]
    when:
      quality: {level: warn}

  # One product with a business bound and a slower queue.
  - match: {domain: sales, product: order_revenue}
    notify: [sales, on-call]
    when:
      freshness: {warn_after: 26h, critical_after: 30h}
      missed_run: {grace: 30m}

  # A product still being built: no freshness alert yet.
  - match: {domain: supply_chain, product: inventory_reliability}
    when:
      freshness: false
```

- **`routes`** maps a route name to a receiver name of `openlakeforge.yaml`.
  Rules refer to routes, never to receivers or URLs.
- **`defaults`** holds the `stages` and `notify` a rule uses when it names none.
  Without `defaults.stages` a rule applies to every stage; a rule that ends up
  with no route is a validation error.
- **`match`** names a `domain` and a `product` from `governance.yml`, or `"*"`
  for either. A job that belongs to no product, such as `gold_maintenance`, is
  matched only by a rule whose `domain` and `product` are both `"*"`.
- **`when`** lists conditions. `{}` turns a condition on with its defaults and
  `false` turns it off.

### Which rule wins

For each stage and condition, the most specific matching rule wins: one that
names a product, then one that names a domain with `product: "*"`, then the
global `"*"`/`"*"`. The choice is per condition, so the `order_revenue` rule
above sets only `freshness` and `missed_run` and still takes `run_failed` from
the global rule. A `false` wins like any other value, which is how a product
opts out of a condition a broader rule turns on.

`notify` belongs to the winning rule alone, not to the union of every rule that
matches. To add `on-call` to a more specific rule, list it there too, as the
`order_revenue` rule does. Two rules of the same specificity that set the same
condition for an overlapping stage are a validation error, never settled by
file order:

```text
alerts.yml: rules[0] and rules[2] have the same specificity and both set ['run_failed'] for stage(s) ['prod']; make one more specific or split their stages
```

### See what a product or a job gets

`olf alert explain` prints, for one product or one job in one stage, the rule
that wins for each condition, its options and the routes it notifies. It reads
`alerts.yml` and `governance.yml` and needs no cluster or credential:

```bash
olf alert explain --stage prod --product order_revenue
```

```text
order_revenue (domain sales, job order_revenue_pipeline) in prod
  run_failed: severity=critical; rules[0] (*/*); notifies on-call -> on-call
  missed_run: grace=30m, severity=critical; rules[2] (sales/order_revenue); notifies sales -> sales-webhook, on-call -> on-call
  freshness: warn_after=26h, critical_after=30h; rules[2] (sales/order_revenue); notifies sales -> sales-webhook, on-call -> on-call
  quality: level=warn; rules[1] (sales/*); notifies sales -> sales-webhook
  schedule_mismatch: severity=warning; rules[0] (*/*); notifies on-call -> on-call
```

A job of no product, such as `gold_maintenance`, takes `--job` in place of
`--product`. It resolves as the evaluator resolves it: by the global rules
alone, those whose `match` is `{domain: "*", product: "*"}`. A job named
`<product>_pipeline` resolves as that product:

```bash
olf alert explain --stage prod --job gold_maintenance
```

```text
job gold_maintenance (no product: global rules only) in prod
  run_failed: severity=critical; rules[0] (*/*); notifies on-call -> on-call
  missed_run: grace=15m, severity=critical; rules[0] (*/*); notifies on-call -> on-call
  freshness: grace=15m, severity=critical; rules[0] (*/*); notifies on-call -> on-call
  quality: level=error; rules[0] (*/*); notifies on-call -> on-call
  schedule_mismatch: severity=warning; rules[0] (*/*); notifies on-call -> on-call
```

`olf` reads no cluster, so it does not check that the job exists.

A condition no rule names prints `not declared, does not alert`, and one a rule
turned off prints `off by` that rule. `--project` selects another project root.

| Exit | Meaning |
| --- | --- |
| `0` | The policy was read, including a project with no `alerts.yml`, which prints that nothing alerts. |
| `1` | The project, `governance.yml` or `alerts.yml` is invalid, or the product is not in `governance.yml`. |
| `2` | The stage is not `dev`, `uat` or `prod`, or not exactly one of `--product` and `--job` is given. |

### Each condition's options

| `when` key | Options | Defaults |
| --- | --- | --- |
| `run_failed` | `severity`: `warning` or `critical` | `critical` |
| `missed_run` | `grace`: how long after a due time a run may still start; `severity` | `15m`; `critical` |
| `freshness` | Either `warn_after` and `critical_after`, or `grace` and `severity` | cadence-derived; see below |
| `quality` | `level`: `warn` or `error` | `warn` |
| `schedule_mismatch` | `severity` | `warning` |

A duration is a positive integer with `m`, `h` or `d`: `30m`, `26h`, `2d`.

- **`missed_run`** with a `grace` of `30m` waits 30 minutes past a due time
  instead of 15 before it opens. Detection still waits for the next 5 minute
  evaluation.
- **`freshness`** opens a `stale_output` incident. With no options it uses the
  schedule's cadence: no success since the previous due time plus the grace.
  With `warn_after` or `critical_after`, it measures the age of the last
  successful run: older than `warn_after` is a `warning`, older than
  `critical_after` is `critical`, and either may be set alone. `warn_after` must
  be below `critical_after`, and the thresholds do not mix with `grace` or
  `severity` in one rule. Thresholds apply whatever the schedule's state, so DEV
  and UAT, which have no schedule, can use them.
- **`quality`** with `level: warn` alerts on a Floe warning or a non-blocking
  error at `warning` and on a blocking error at `critical`. With
  `level: error`, a warning alone does not alert. Floe and dbt keep their own
  thresholds; `level` only chooses which outcome alerts.
- **`schedule_mismatch`** applies to PROD, the only stage with schedules.

## Platform alerts

Four conditions describe the platform and not a product. `alerts.yml` cannot
name them; the schema rejects them as unknown `when` keys. They go to the
[platform receivers](#platform-receivers), capped by each receiver's `stages`,
and open and resolve under the same rules as other incidents, with no
reminders.

| Condition | Severity | Opens when |
| --- | --- | --- |
| `stage_unreadable` | `critical` | Dagster for a stage could not be read in 3 consecutive evaluations. |
| `policy_unreadable` | `warning` | A stage's alert policy cannot be used. |
| `service_degraded` | `warning` or `critical` | A platform service of a stage is degraded. One incident per stage and component. |
| `health_unreadable` | `critical` | The platform API's services route for a stage could not be read in 3 consecutive evaluations. |

`service_degraded` and `health_unreadable` need the platform API, which the
local provider deploys when the profile declares `spec.access`. Without it
the evaluator reads no service signal and raises neither.

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
ends with. The first five are product conditions and alert only where
`alerts.yml` declares them; the severities below are their defaults, which a
rule's options change.

### run_failed

Critical. The latest run of a job failed. A canceled run does not alert.
Open `links.run`, read the failed step, and fix or rerun. It resolves when the
latest run succeeds; a run still in progress does not resolve it. See
[troubleshooting](../troubleshooting.md).

### missed_run

Critical. A running schedule passed its due time plus its grace (15 minutes
unless the rule sets `grace`) and no run started, which usually means the Dagster scheduler is down. Check the
scheduler daemon in the stage's Dagster. It resolves when a run starts.
Stopping the schedule closes it without a notification only when the product's
cadence does not declare that schedule running. If the cadence declares it
running, stopping it leaves `missed_run` open and also opens a
`schedule_mismatch`; start a run, or change the declaration first.

### stale_output

Critical, declared as `freshness`. Without thresholds, a running schedule's job
has had no success since the previous due time plus the grace (15 minutes
unless the rule sets `grace`). With `warn_after` or `critical_after`, the last
success is older than that bound, at `warning` or `critical`, whatever the
schedule's state; a job with no success among the last 25 runs opens nothing. It
is not sent while `run_failed` or
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
stopped in Dagster. Start it, or change the declaration. Starting the schedule
resolves the incident with a `resolved` notification; changing the declaration
so the schedule is no longer expected to run closes it without a
notification.

### stage_unreadable

Critical, to the platform receivers only. Dagster for a stage could not be
read in 3 consecutive evaluations. Check that the stage's Dagster webserver is
running. While it lasts, that stage's open incidents are held, not resolved.

### policy_unreadable

Warning, to the platform receivers only. The evaluator could not use a stage's
alert policy. The causes are a failed read of the stage's activation or
revision, a revision file whose digest does not match its sidecar, an
`alerts.yml` that fails validation (for example a revision built by a newer
distribution than the evaluator), a revision that has `alerts.yml` without
`governance.yml`, and a route that names a receiver the profile no longer
declares. The notification carries a fixed summary, not the cause: the
evaluator logs the cause per stage (see [Check it](#check-it) for the Job's
log). What happens meanwhile depends on whether the stage has a last good
policy:

- **A last good policy exists.** The evaluator keeps applying it, so alerts
  continue as before.
- **None does** (the first evaluation after a deploy, or after the cache was
  lost). The stage is left out of the evaluation: its open product incidents are
  held, not resolved, nothing new opens for it, and a queued product
  notification waits. A policy that cannot be read is not a rule that went away.

For a stale route, restore the receiver in `openlakeforge.yaml` and run
`olf infra platform apply`, or change the route in `alerts.yml` and deploy a new
revision; the other routes keep delivering. For a policy that fails validation,
run `olf check contracts` on the revision's source and deploy a fixed one. The
incident resolves, with a notification, on the first evaluation that reads the
stage's policy cleanly. A stage with no activation, or whose revision has no
`alerts.yml`, is a normal state and raises nothing.

### service_degraded

Warning or critical, one incident per stage and component, sent to the platform
receivers only. A Dagster or Trino workload that is not ready alerts at
`warning` or `critical`, as `GET /api/v1/stages/{stage}/health/services`
reports it. The object store that cannot be listed and the log archive's spool age, spool size
and newest object alert at `critical` only. The incident resolves when the
component reports `healthy`; an `unknown` or `disabled` component changes
nothing. There are no reminders. Read the component's `reason` in the health
routes' `services` list.

### health_unreadable

Critical, to the platform receivers only. The platform API's services route
(`GET /api/v1/stages/{stage}/health/services`) could not be read for a stage in
3 consecutive evaluations. Check that the
`platform-api` pod is running. While it lasts, open `service_degraded` incidents
stay open: an API that cannot answer is not evidence that the services recovered.

## Check it

The health routes do not depend on receivers: with none declared,
`GET /api/v1/stages/{stage}/health` still reports a failed run as `critical`.

Failed evaluations stay visible as Jobs, and each run logs one summary line:

```bash
kubectl -n olf-system get jobs -l openlakeforge.io/job=alert-evaluator
JOB=$(kubectl -n olf-system get jobs -l openlakeforge.io/job=alert-evaluator \
  --sort-by=.metadata.creationTimestamp -o name | tail -n 1)
kubectl -n olf-system logs "$JOB"
```

## The limit of an in-cluster evaluator

The evaluator runs inside the cluster it watches. A whole-cluster outage, a
node pool that cannot schedule `olf-system`, a stopped CronJob controller or an
unreachable object store produces no alert. If it cannot read or write its
state it delivers nothing and the Job fails. To catch those cases, watch from
outside: the state object `alerts/state/v1/state.json` carries `evaluated_at`,
the time of the last evaluation, so a monitor that polls it can tell a stopped
evaluator from a quiet one.
