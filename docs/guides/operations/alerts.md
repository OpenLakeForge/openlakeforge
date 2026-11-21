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
`runbook`, the [runbook section](#what-to-do-for-each-condition) of its
condition as its notification links it (`null` where the deployment sets no
runbook page), and its latest notification's `delivery`:

| `status` | Meaning |
| --- | --- |
| `pending` | Queued: not every receiver routed to it has it yet |
| `delivered` | A receiver routed to it accepted it, and none rejected it |
| `undeliverable` | It was given up after 12 evaluations, or a receiver rejected it |
| `unrouted` | No receiver was, or still is, routed to it, so nobody was told: its receivers may have been removed from the routes while it was retried |
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
| `links.runbook` | The condition's section of the alert runbook: [What to do for each condition](#what-to-do-for-each-condition) at the deployed release, unless [overridden](#the-runbook-link). |

A payload never carries log lines, run tags, error text, row contents, storage
URLs or credentials.

## What to do for each condition

This is the alert runbook. The headings are the `condition` values, which is
the anchor `links.runbook` ends with. The platform API links the same sections:
each incident from `GET /api/v1/stages/{stage}/alerts/incidents` carries
`runbook`, its condition's section, and the run quality answer
(`GET /api/v1/stages/{stage}/runs/{run_id}/quality`) links the
[`quality`](#quality) section for a run with an error check and the
[`run_failed`](#run_failed) section for any other failed run. The first five
are product conditions and alert only where `alerts.yml` declares them; the
severities below are their defaults, which a rule's options change.

Each section says first what to check in the Portal, then the command line or
API read that shows the same thing. For a product condition, start with the
stage, then the run; [Walk through a failed run](monitoring-walkthrough.md)
shows each screen:

1. Open **Monitoring** in the Portal and select the stage. **Needs attention**
   lists the open incident beside its pipeline's signal, with its latest runs
   and whether its notification went out. The **Pipelines** table gives each
   pipeline's state, latest run, last success, schedule and failed runs.
2. Select a run to open its page. **What failed** names the
   failed step and the first line of its error, and links **Open in Dagster**,
   the **Runbook** and **All runs of** the job. **Monitoring → Runs** lists the
   stage's runs by pipeline, product, status and window.
3. The run's **Logs** card reads the run's window from the log archive, after
   its pods are gone. **Open in Logs ↗** opens the same window on the **Logs**
   page, which filters by level, pod and text.
4. The run's **Quality checks** card lists the Floe and dbt checks that did not
   pass, and **What failed** links the run's retained reports.

The same reads on the command line need `olf login` and go through the
platform API:

1. `olf pipelines health <stage>` prints each pipeline's state and the reason.
2. `olf runs list --stage <stage> --job <job>` lists the job's latest runs, and
   `olf runs show <run-id> --stage <stage>` one run; `links.run` opens the same
   run in the stage's Dagster, with its steps and event log.
3. `olf logs --stage <stage> --run-id <run-id> --severity error` prints the
   run's archived pod log lines; the [log archive](log-archive.md) guide covers
   the other filters.
4. `GET /api/v1/stages/{stage}/runs/{run_id}/quality` lists the run's quality
   checks and the reports it retained.

The Portal does not start runs. To rerun a job after a fix, re-execute the run
in the stage's Dagster (**Open in Dagster**), use
`olf runs launch <job> --stage dev` on DEV, or
`olf infra product run <product> --stage <stage>` with cluster access on any
stage.

### run_failed

Critical. The latest run of a job failed. A canceled run does not alert.

- **In the Portal:** the incident's run in **Needs attention** opens the
  run's page. **What failed** gives the failed step and its error,
  the **Logs** card the run's error lines, and **Quality checks** whether a
  blocking check stopped the run.
- **On the command line:** the failed step in `links.run`, then the run's error
  lines in `olf logs`.
- **Common causes:** an error in the product's code, a source that is
  unreachable or misses a bound key (`olf infra source bindings --stage <stage>`
  lists missing keys), a blocking quality check (see [`quality`](#quality)),
  or a pod that was evicted or ran out of memory.
- **Recovery:** fix the cause and rerun. It resolves when the latest run
  succeeds; a run still in progress does not resolve it. See
  [troubleshooting](../troubleshooting.md) for pods that do not start.

### missed_run

Critical. A running schedule passed its due time plus its grace (15 minutes
unless the rule sets `grace`) and no run started.

- **In the Portal:** **Needs attention** shows *Expected run did not start*
  with the time the run was due, and the pipeline's **Schedule** in the
  **Pipelines** table reads **Running**. The **Services** table lists the
  stage's Dagster when one of its workloads is not ready, with the reason.
- **On the command line:** the scheduler daemon in the stage's Dagster, and its
  pods: `kubectl -n <stage-namespace> get pods -l app.kubernetes.io/name=dagster`.
- **Common causes:** the Dagster daemon is down, scaled to zero or cannot be
  scheduled.
- **Recovery:** bring the daemon back, or start a run. It resolves when a run
  starts. Stopping the schedule closes it without a notification only when the
  product's cadence does not declare that schedule running. If the cadence
  declares it running, stopping it leaves `missed_run` open and also opens a
  `schedule_mismatch`; start a run, or change the declaration first.

### stale_output

Critical, declared as `freshness`. Without thresholds, a running schedule's job
has had no success since the previous due time plus the grace (15 minutes
unless the rule sets `grace`). With `warn_after` or `critical_after`, the last
success is older than that bound, at `warning` or `critical`, whatever the
schedule's state; a job with no success among the last 25 runs opens nothing. It
is not sent while `run_failed` or `missed_run` is open for the same product,
since either already explains it.

- **In the Portal:** **Needs attention** shows *Data is late* or *Data is
  ageing*. In the **Pipelines** table, compare the pipeline's **Last success**
  with its **Latest run**, and open its **Failed runs**, or all its runs in
  **Monitoring → Runs**, to see whether runs are being canceled.
- **On the command line:** `olf pipelines health <stage>` and the job's latest
  runs in `olf runs list`.
- **Common causes:** runs that keep being canceled, a schedule stopped in a
  stage where the rule sets thresholds, or thresholds tighter than the
  product's real cadence.
- **Recovery:** a successful run resolves it. The signal is described in
  [health signals](../../reference/health-signals.md).

### quality

`warning` for a Floe warning or a non-blocking error, `critical` for a blocking
error. It is one incident whose severity can rise, so a warning that becomes a
blocking error sends one `escalated`.

- **In the Portal:** the run's **Quality checks** card lists the checks that did
  not pass, errors first, with the table and column, what the pipeline did
  (quarantined rows, blocked the run, continued) and the counts the engine
  recorded. **Floe report ↗** and **dbt results ↗** under **What failed**
  download the reports. The overview's **Data quality** table gives each
  product's latest checks.
- **On the command line:** the run's checks and reports from the run quality
  route above: which engine, table and check, and the rows it rejected or the
  failures it counted. `olf logs --stage <stage> --run-id <run-id> --component
  floe` shows the Floe validation's own lines.
- **Common causes:** source data that changed shape or content (nulls, values
  that do not cast, duplicate keys), or a Floe contract or dbt test that no
  longer matches the data.
- **Recovery:** fix the data at its source or the contract or test, then rerun.
  It resolves when a later run's complete quality manifest passes. Thresholds
  stay in your Floe and dbt configuration.

### schedule_mismatch

Warning. A PROD schedule that the product's cadence declares running is
stopped in Dagster.

- **In the Portal:** in the PROD stage's **Pipelines** table, the pipeline's
  **Schedule** reads **Stopped**, with the schedule's name.
- **On the command line:** the schedule's state in the stage's Dagster.
- **Common causes:** the schedule was stopped by hand, or the declaration was
  not updated when the product's cadence changed.
- **Recovery:** start the schedule, which resolves the incident with a
  `resolved` notification, or change the declaration so the schedule is no
  longer expected to run, which closes it without a notification.

### stage_unreadable

Critical, to the platform receivers only. Dagster for a stage could not be
read in 3 consecutive evaluations. While it lasts, that stage's open incidents
are held, not resolved.

- **In the Portal:** the stage's overview reads its pipelines as unknown, with
  the reason, and the **Services** table shows Dagster's workloads and read API
  with their state and reason.
- **On the command line:** the stage's Dagster webserver:
  `kubectl -n <stage-namespace> get pods -l app.kubernetes.io/name=dagster`,
  and the `dagster.read-api` row of `olf health <stage>`.
- **Common causes:** the webserver is down or restarting, or no code location
  is loaded.
- **Recovery:** see [`dagster.read-api`](service-health.md#dagsterread-api).
  It resolves on the first evaluation that reads the stage.

### policy_unreadable

Warning, to the platform receivers only. The evaluator could not use a stage's
alert policy. The notification carries a fixed summary, not the cause: the
evaluator logs the cause per stage (see [Check it](#check-it) for the Job's
log).

- **In the Portal:** **Needs attention** lists the incident with its summary
  only; the cause is in the evaluator's log, which needs cluster access.
- **Common causes:** a failed read of the stage's activation or revision, a
  revision file whose digest does not match its sidecar, an `alerts.yml` that
  fails validation (for example a revision built by a newer distribution than
  the evaluator), a revision that has `alerts.yml` without `governance.yml`,
  and a route that names a receiver the profile no longer declares.
- **Meanwhile:** with a last good policy, the evaluator keeps applying it, so
  alerts continue as before. With none (the first evaluation after a deploy, or
  after the cache was lost), the stage is left out of the evaluation: its open
  product incidents are held, not resolved, nothing new opens for it, and a
  queued product notification waits. A policy that cannot be read is not a
  rule that went away.
- **Recovery:** for a stale route, restore the receiver in `openlakeforge.yaml`
  and run `olf infra platform apply`, or change the route in `alerts.yml` and
  deploy a new revision; the other routes keep delivering. For a policy that
  fails validation, run `olf check contracts` on the revision's source and
  deploy a fixed one. The incident resolves, with a notification, on the first
  evaluation that reads the stage's policy cleanly. A stage with no activation,
  or whose revision has no `alerts.yml`, is a normal state and raises nothing.

### service_degraded

Warning or critical, one incident per stage and component, sent to the platform
receivers only. A Dagster or Trino workload that is not ready alerts at
`warning` or `critical`, as `GET /api/v1/stages/{stage}/health/services`
reports it. The object store that cannot be listed and the log archive's spool
age, spool size and newest object alert at `critical` only.

- **In the Portal:** the overview's **Services** table lists the service that is
  not healthy, with each component's state and reason. With metrics on,
  **Monitoring → Metrics** charts each component's CPU and memory.
- **On the command line:** `olf health <stage>`, and the component's `reason`
  there.
- **Common causes and recovery:** per component in
  [service health](service-health.md#what-to-do-for-each-component).
- **Resolves** when the component reports `healthy`; an `unknown` or `disabled`
  component changes nothing. There are no reminders.

### health_unreadable

Critical, to the platform receivers only. The platform API's services route
(`GET /api/v1/stages/{stage}/health/services`) could not be read for a stage in
3 consecutive evaluations. While it lasts, open `service_degraded` incidents
stay open: an API that cannot answer is not evidence that the services
recovered.

- **In the Portal:** the Portal reads the same API, so a stage overview that
  cannot read its services points at the platform API rather than a service.
  The checks below need cluster access.
- **On the command line:** the `platform-api` pod:
  `kubectl -n olf-system get pods -l app.kubernetes.io/name=platform-api`.
- **Common causes:** the pod is not ready or restarting, or its settings were
  refused at startup, which its log names.
- **Recovery:** it resolves on the first evaluation that reads the route.

### The runbook link

The local provider sets the runbook page to this guide on GitHub at `main`,
`https://github.com/OpenLakeForge/openlakeforge/blob/main/docs/guides/operations/alerts.md`.
Not the release tag: a build between releases is newer than its version's tag,
and a page or section added since would not be there. To link your own runbook
page, set the platform root's `runbook_base_url` in a variable file and deploy
with it:

```hcl
runbook_base_url = "https://wiki.example.com/data-platform/alerts"
```

```bash
olf infra deploy --var-file runbook.tfvars
```

The URL must be `https` and carry no `#fragment`: the link appends
`#<condition>`, so your page needs a section with each condition's name as its
anchor.

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
