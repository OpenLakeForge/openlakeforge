# Health signals

How OpenLakeForge classifies a pipeline or data product as healthy, warning,
critical, unknown or disabled, and why `unknown` and `critical` are computed
the way they are. The rules live in `packages/core/openlakeforge_core/health.py`
and perform no I/O: a consumer reads Dagster, Floe, dbt or the object store and
passes what it saw in.

## States

| State | Meaning |
| --- | --- |
| `healthy` | A fresh observation shows nothing wrong |
| `warning` | Needs attention, nothing is blocked |
| `critical` | Something failed or did not happen |
| `unknown` | No fresh observation, so nothing can be claimed |
| `disabled` | Intentionally not running |

## Signal kinds

A signal has a kind, a state, a stage, optional domain, product, table and
run, `observed_at`, `expires_at`, a short `reason` and an opaque `evidence_uri`
that points at the retained evidence rather than carrying log text.

An `expected_run` signal also carries `due`, the cron occurrence it judged, when
a run was actually expected at it. The pipeline health route returns it on each
signal: null on every other kind, and null on an `expected_run` that judged no
occurrence (an `unknown` signal, a stopped or absent schedule, or a schedule
switched on after the occurrence). It is never filled from the schedule's
`last_expected_at`, which is the cron's latest occurrence whether or not a run
was expected at it.

| Kind | Computed from |
| --- | --- |
| `execution` | Status of the latest run |
| `latest_success` | Whether a successful run is on record |
| `expected_run` | The schedule state, the run it expected, and whether it is stopped against its declared cadence |
| `freshness` | Whether a successful run is on record since the schedule's owed due time (the latest one at least a grace old) |
| `quality` | A Floe or dbt outcome, read from the latest run's quality manifest (see below) |
| `service` | Controller readiness of a component, or a probe's answer |

## What a service row says about itself

The stage health route returns each service with its `component`, a `source` (what the probe asked: `kubernetes`,
`dagster-graphql`, `trino-http`, `polaris-http`, `s3`, `log-collector` or `quality-reconciler`; never an address), `observed_at`, `expires_at` and
`max_age_seconds`, the gap between the two. Probes run when the route is read and are reused for a few seconds, so
`max_age_seconds` says how long an answer stays usable; it is not a polling interval.

Each row also carries `display_name` (such as `Dagster API`), `service` (one of `dagster`, `trino`, `polaris`,
`object-store`, `log-archive`, `quality-sync`, `platform-api`) and `service_display_name`, so a client can group rows by
service without parsing `component`, which stays the key to match on. `last_success_at` is the component's own evidence
of its last success: the newest archive object's write time on `log-archive.newest-object`, the last published result
on `quality-sync.publication`. It is null on every other row, when there is no such evidence or it could not be read,
and whenever the row is `unknown` or `disabled`.

| Component | Source | `healthy` means | Does not mean |
| --- | --- | --- | --- |
| `dagster.workloads`, `trino.workloads` | `kubernetes` | Pods ready, schedulable, not crash looping, and no matching Deployment or StatefulSet scaled to zero | The service answers |
| `dagster.read-api` | `dagster-graphql` | The webserver answers with a code location loaded | A run would launch |
| `trino.readiness` | `trino-http` | The coordinator finished starting | Workers, catalogs or Polaris access work |
| `polaris.readiness` | `polaris-http` | The catalog's management port answers `/q/health/ready` as UP, sent with no credential | A catalog read works, a grant holds, or the stage's catalog exists |
| `object-store.list` | `s3` | The endpoint is reachable and the key may list the bucket | A write is durable |
| `quality-sync.publication` | `quality-reconciler` | The reconciler's last pass is recent and no result waits too long or gave up ([below](#publication-health)) | The data passed its checks, or OpenMetadata shows a given result |

`polaris.readiness` is `critical` when Polaris answers that it is not ready (HTTP 503 or a DOWN status), and `unknown`
when it does not answer, answers something else or the probe times out. It reads `disabled` in a profile that deploys no
catalog. Polaris is one service for every stage, so each stage's health shows the same answer. The probe holds no
Polaris principal, so a `healthy` row says the service is up and nothing about the data plane.

The `metrics` integration says whether the metrics backend ([ADR 0024](../adr/0024-metrics-prometheus-reference-and-read-path.md))
answers: `disabled` when the profile turns metrics off (Slim, by default), `not_provided` when they are on but the
provider has no backend, `unavailable` when the backend does not answer one query within 5 seconds (a single attempt,
so a rate-limited backend reads `unavailable` at once), and `available`
otherwise. It never changes the stage's state and blanks nothing: the service rows do not depend on it.

## How unknown is computed

An observation is usable for `max_age` after `observed_at`. A missing
observation, or one at least `max_age` old, gives `unknown` for `execution`,
`latest_success`, `expected_run`, `quality` and workload `service` signals,
whatever the old observation said. A quality outcome of `not_run` or `unknown`
gives `unknown` too. A failed fetch is therefore never green.
`max_age` has no default; the caller sets it from how often it observes.
`freshness` takes no `max_age`, because its bound is the schedule's cadence;
its `unknown` cases are listed below.

For a pipeline with a running schedule the platform API sets `max_age` for
`execution` and `latest_success` to the gap from the owed due time (defined
below) to the next due time after it, plus the grace, both read from the cron.
A success therefore stays evidence until the next due time after it plus the
grace: a `0 9 * * 1-5` pipeline's Friday success is still evidence on Saturday
and Sunday (the bound is 72 hours and 15 minutes at Friday's owed due time),
and a daily one's is 24 hours and 15 minutes. A pipeline with no running
schedule, or whose cron cannot be read, keeps the provisional seven days
(`RUN_MAX_AGE`, #1127).

## How freshness is computed

Freshness asks whether the pipeline kept its own cadence. It invents no
threshold: an SLA is #921 (v0.7.5), so the only bound is the schedule's cron.
`evaluate_freshness` takes the latest successful run, the due time whose run is
owed, `now`, the grace and the schedule state.

The platform API derives the owed due time as the latest due time at or before
`now` minus the grace (`latest_due`: croniter in the schedule's timezone with
Dagster's daylight-saving rules, never from Dagster ticks). It is computed
directly from the cron, not by stepping back one occurrence, so a schedule that
fires more often than the grace (`* * * * *`, `*/5 * * * *`) owes the run due a
grace ago and reads stale once its runs stop. A due time still inside its grace
is never owed, so it cannot hide a missed earlier one. The next due time after
it comes from `next_due`, the same walk run forward.

| Observation | State |
| --- | --- |
| No schedule (a manual pipeline), or a stopped one | `disabled`, never stale |
| A run succeeded at or after the owed due time | `healthy` |
| No success since the owed due time (its grace has elapsed by construction) | `critical` (stale) |
| The schedule was switched on after the owed due time, so it owes no run yet | `healthy`, "no scheduled run due yet; next run due 02:00 UTC tomorrow" |
| No successful run in the runs read (the latest 25 per job) | `unknown` |
| The cron or its timezone cannot be read | `unknown`, naming the cron and timezone |
| Dagster records no start time and nothing shows the schedule was on at the owed due time | `unknown` |

A schedule switched on after its latest owed due time (a stage activated after
the day's run, or a schedule turned back on) owes its first run at the first
due time at or after its start, which Dagster records as the schedule's start
time. Until that run's grace elapses the reason gives the first due time in
UTC, as `today` or `tomorrow` by the UTC date, and once the time itself has
passed it says the first run is within its grace period. A cron with several
ticks a day owes the first tick after the start, not the first tick of the
next day. The alert evaluator neither opens nor resolves a stale-output
incident on this reason: nothing is owed, but the output is no fresher than it
was.

Stale is `critical`, as ADR 0023 has `stale_output` alert as critical: output
that has not refreshed since its owed due time is a broken promise to its
consumers. A stale pipeline whose run also failed, or never started, is already
`critical` through `execution` or `expected_run`; the stage rollup takes the
worst signal, so that adds no new state. The alert evaluator (ADR 0023,
local provider only) does not open a stale-output alert while a failed-run or
missed-run alert is open for the same product, so one cause notifies once
(`_STALE_CAUSES` in `openlakeforge_core/alerts.py`). A success counts by the time it ended, and the latest
completed success is the one read, so overlapping runs that finish out of
creation order are judged by the one that finished last. The signal
carries no `expires_at`, since it expires at the next due time, which the module
does not know.

## How critical is computed

| Observation | State |
| --- | --- |
| Latest run failed | `critical` |
| Schedule running, a run was due at least the grace period ago, and no run started since | `critical` |
| Quality ERROR that blocks the pipeline | `critical` |
| A component with no ready pod, or with an unschedulable pod | `critical` |

A missed run is critical without any failure event, because a stopped
scheduler emits none. A schedule that is stopped or absent is `disabled`, not
`critical`, with one exception: a stopped schedule that the product's cadence
declares running is a `warning` (see below). The last expected run time comes from the schedule input; the
module does not parse cron.

## Quality signal

When a stage has a quality manifest reader, each product pipeline's latest run
carries one `quality` signal, scoped like its other signals. It is read from
`activations/<stage>/run-artifacts/quality/<domain>/<product>/<run>/manifest.json`,
which every Floe and dbt step of the run rewrites. The domain and product are
the job definition's `openlakeforge/silver-domain` and `openlakeforge/product`
tags, which the health routes also answer as each pipeline's `domain` and
`product`. A job without both, a maintenance job for one, gets no quality
signal; so does a product job deployed before the product tag, until its code
location is redeployed. The job name is never parsed for a product.

| Manifest | `quality` |
| --- | --- |
| A check with outcome `error` that blocked the pipeline | `critical` |
| `warn`, or an `error` that only continued or quarantined rows | `warning` |
| Every check `passed` | `healthy` |
| No manifest, an unreadable or foreign one, or one older than the run's evidence bound | `unknown` |
| Some checks `not_run` or `unknown` and the rest `passed`, or a `complete: false` manifest with every check `passed` | `unknown` |

A manifest carries `complete`, set only by the run's last publish, once dbt's
evidence has been read; an intermediate publish, or one made after dbt's worker
died before uploading evidence, leaves it `false` (absent reads `false`). Only a
`SUCCESS` run's complete manifest can read `healthy`: a pass in an incomplete
one reads `unknown`, because dbt's results are added last. Runs are read newest
first. One that did not succeed answers only with a `warning` or `critical`,
and is otherwise passed over, so a failure between an old success and a
Silver-only retry still speaks. The first `SUCCESS` run answers with its own
manifest and the walk stops there. A newer run can stop that success from
reading `healthy`: one whose manifest exists but cannot be read (store error,
malformed, foreign) may be hiding a failure, and one that ended `FAILURE` or
`CANCELED` without a complete manifest never finished its checks, so the older
pass is not the current quality. The newest such run answers `unknown` in its
place, with its own run id and, for the second kind, the reason "the run failed
before its quality checks finished" (or "was canceled"). A run still in progress
hides nothing, since it may yet finish. When no success is reached, that run
answers the same way; with neither, the pipeline carries no `quality` signal.
`unknown` opens no incident and resolves none, so either leaves an open
incident open. A manifest
whose `results` are malformed (an entry that is not an object, wrong field
types) reads `unknown` like an unreadable one.

The worst check decides, and no table name is used, so a manifest written from
the baked dbt manifest, which names no table, reads the same as any other. A
stage without the reader adds no signal and reports the `quality` integration
as `not_provided`; with it the integration is `available`. The reader's key is
`Read` and `List` on that stage's `run-artifacts/quality/` prefix only
(`quality_reader_credentials` in `modules/storage/seaweedfs`).

`GET /api/v1/stages/{stage}/health/quality` counts the results of the manifest
that decided each pipeline's `quality` signal, by outcome, with `blocking` (the
errors that blocked the pipeline) beside `error` rather than added to it. It
reads in the same pass as `/health/pipelines`, so its `state` is that signal,
and a pipeline the walk above reached no run for reads `unknown` with no run.
Counts are `null` where the manifest is absent or unusable, never zeros, and an
incomplete manifest keeps its counts with `complete: false`. A run that ended
before its checks finished is the row's `run_id`, with its partial counts or
none, never the older success's.

`GET /api/v1/stages/{stage}/runs/{run_id}/quality` lists every check of one
run's manifest, which is never re-run: engine, check id, table, column,
outcome, what it did to the pipeline, and the counts the engine recorded, `null`
where it recorded none. A run without a usable manifest has `checks: null` and
`counts: null`, with the `quality` source saying why; never an empty list or
invented passes. Each report the run retained, a Floe entity's run report or
dbt's run results, is listed once with an `href` to
`.../quality/reports/{report_id}`, which streams it through the platform API
with the files key; a presigned object-store URL is never handed out. The
download route's floor is the logs route's, so a `data-engineer` gets `href`
`null` outside DEV. `runbook` links the alert runbook's section for the
condition the run would alert on: [`quality`](../guides/operations/alerts.md#quality)
when a check ended in `error`, else [`run_failed`](../guides/operations/alerts.md#run_failed)
for a failed run. It is `null` for a run that passed or only warned, and
where the deployment sets no runbook page.

## Quality publication to OpenMetadata

Publishing a run's quality manifest to OpenMetadata is separate from the
quality signal above: the signal says whether the data passed, publication says
whether the catalog has caught up. A failed publication never changes the
quality signal or a run's status (ADR 0007).

The publisher (`openlakeforge_services/quality_publisher.py`) keeps, under the
stage's activation prefix, one state object per run at
`quality-sync/state/<domain>/<product>/<run>.json` and one summary at
`quality-sync/summary.json`. The summary is the object a health reader takes:

| Field | Meaning |
| --- | --- |
| `pending` | Results still to publish, including those waiting for a table to be registered |
| `failed` | Results given up on after 10 attempts; they wait for `--retry-failed` |
| `lag_seconds` | Age of the oldest unpublished result, null when nothing waits |
| `last_success_at`, `last_failure_at` | Times of the last published and last failed result |
| `last_error` | One line, never a response body |

A stage without governance has no summary and no publisher; the health route
reads that from its settings, not from a missing object. Retries back
off from 1 minute to 1 hour. OpenMetadata statuses: PASS is `Success`, a blocking ERROR is `Failed`
and an advisory WARN is `Aborted`, because `Failed` opens an incident in
OpenMetadata 1.13.6 and `Success` would hide the warning. The original outcome
and engine severity are in each result's values. `--warn-status Failed` opts
into incidents for WARN.

### Scheduled publication

On the local provider, every stage with governance gets a CronJob in the
platform namespace, `quality-reconciler-<stage>`, that runs the same publisher
every 5 minutes (`modules/governance/quality-reconciler`). It is not rendered
for a stage without governance: Slim has no CronJob, no ServiceAccount, no
key, no Secret and no NetworkPolicy for it.

| Setting | Value |
| --- | --- |
| Image | The platform API image, with the command `python -m openlakeforge_platform_api.quality_sync` |
| Work per pass | At most 25 runs, oldest first; the rest wait for the next pass |
| Overlap | None: a pass still going makes the next one skipped, and it stops after 240 seconds |
| Result | Exit 0 when the pass finished, including results still waiting for a table; 1 when OpenMetadata or the bucket stopped answering, so the failed Job stays visible; 2 when its environment is wrong |
| Retries | Inside the state objects (above), not by the Job: a failed Job is retried by the next schedule |

Each stage's pass is confined to that stage:

- Its ops-bucket key, `openlakeforge-quality-sync-<stage>`, can `Read` and
  `List` that stage's `run-artifacts/quality/` and `Read`, `Write` and `List`
  that stage's `quality-sync/`. It cannot read another stage's evidence, write a
  manifest, or reach anything else in the bucket.
- The pass is configured with that stage's activation prefix and database root
  (`polaris.lakehouse_<stage>`). A prefix or root of another stage is refused
  before any request, so a DEV pass cannot write PROD tables or PROD state.
- Its egress is DNS, OpenMetadata on 8585 and the object store on 8333.
  Nothing can connect to its pods, and it holds no Kubernetes API token.
- It publishes as the stage's own OpenMetadata bot, `olf-quality-writer-<stage>`,
  which may read tables and write test definitions, cases and results, and
  nothing else (ADR 0007, Identity). Its token is read from the
  `openmetadata-quality-writer-<stage>` Secret and is never an argument, a
  literal environment value or a log line. The Secret is deleted when the
  stage loses governance, including when every stage does. OpenMetadata
  cannot scope a role to one stage's tables, so the stage boundary above is
  enforced by the key and the pass's configuration, not by the token.

Inspect or trigger a pass:

```bash
kubectl -n olf-system get cronjob -l app.kubernetes.io/name=quality-reconciler
kubectl -n olf-system create job --from=cronjob/quality-reconciler-dev quality-reconciler-dev-now
kubectl -n olf-system logs job/quality-reconciler-dev-now
```

Until `olf infra openmetadata bootstrap` has written the stage's
`openmetadata-quality-writer-<stage>` Secret, a pass's pod cannot start (`CreateContainerConfigError`) and its Job
fails at the 240-second deadline; passes publish once the Secret exists. The
AWS and Azure roots do not deploy the reconciler yet, so there the command
below is the only way to publish.

### Publication health

The stage health route reads each governed stage's summary as the service
signal `quality-sync.publication`, apart from the `quality` signal: a lagging
publisher never changes whether the data passed. The worst finding decides:

| Finding | State |
| --- | --- |
| The summary is at least 15 minutes old (two scheduled passes missed) | `warning` |
| The summary is at least 1 hour old | `critical` |
| The oldest unpublished result has waited at least 30 minutes | `warning` |
| It has waited at least 2 hours (the retry backoff has reached its 1-hour cap) | `critical` |
| A result gave up after 10 attempts (`failed` above 0) | `critical` |
| A manifest cannot be published at all (`rejected_runs` above 0) | `warning` |
| None of the above | `healthy` |
| No summary, a summary that does not parse or is another stage's, or a read that fails | `unknown` |
| A stage without governance | `disabled` |

The thresholds follow the schedule: a pass every 5 minutes that stops after
240 seconds leaves a summary at most about 9 minutes old, and a result is
published by the first pass after its manifest lands. A result waiting for its
table to be registered counts toward the lag like any other. Lag and age are
measured against the API's clock when the route is read, so a reconciler that
stopped keeps aging. The `quality-sync` integration reads `available` on a
governed stage and `not_provided` on one without governance, as on Slim, where
no publisher exists to fail.

The read uses the platform API's files key, which already holds `Read` on the
whole ops bucket for the files explorer; no grant, Secret or flow was added.
The settings name each governed stage's summary
(`quality_sync_summaries`, rendered by `modules/access/platform-api`), so the
key reads only that object for this signal. The signal does not raise an
alert: `service_degraded` covers the components ADR 0023 lists.

To publish without rerunning a pipeline, for a run that failed to publish or
one older than the 30-day scan window:

```bash
uv run --project tools/olf olf infra openmetadata quality-sync --stage dev --limit 25
uv run --project tools/olf olf infra openmetadata quality-sync --stage dev --run <dagster-run-id>
uv run --project tools/olf olf infra openmetadata quality-sync --stage dev --retry-failed
```

A result already in OpenMetadata is never published twice. A table that is
not registered keeps its results pending; the command never creates one.

## Schedule mismatch

`expected_run` compares the schedule's state with what the code location
declares. Dagster reports both: `status` is the stored state, which a toggle in
the Dagster UI overrides, and `defaultStatus` is the `default_status` the code
declared, which `ProductCadence.enabled_in_prod` sets. `declared_running` on
`ScheduleObservation` carries the second.

| Declared (`defaultStatus`) | Actual (`status`) | `expected_run` |
| --- | --- | --- |
| `RUNNING` | `STOPPED` | `warning`: the schedule does not match its cadence |
| `STOPPED` | `STOPPED` | `disabled` |
| `RUNNING` | `RUNNING` | judged as a running schedule (`healthy`, or `critical` for a missed run) |
| `STOPPED` | `RUNNING` | judged as a running schedule; the UI toggle wins and is not reported |

A `warning` here is what the alert model reads as `schedule_mismatch`. It is
`warning`, not `critical`, because nothing has failed: someone switched the
schedule off, or its default was ignored. A schedule that is absent is
`disabled` whatever the cadence says. `freshness` stays `disabled` for a
stopped schedule, so the mismatch shows once, on `expected_run`.

Other outcomes: a canceled run, a quality WARN or non-blocking ERROR, and a
component that is scaled to zero, partly ready, crash looping or restarting
repeatedly give `warning`; a run in progress and a passing check give `healthy`.

## Service signals

A `service` signal comes from one probe of one component of a stage (ADR 0021, decision 7). Each carries
`observed_at` and an `expires_at` of 60 seconds later; the CLI prints both. A probe that errors, times out or is
refused reads `unknown`, a component that is not deployed reads `disabled`, and the probes run for at most 5 seconds
and are reused for 10. The components, what each proves and what to do are in the
[service health guide](../guides/operations/service-health.md).

## Health and the alert policy

The health routes do not read the alert policy. `GET /api/v1/stages/{stage}/health`,
`GET /api/v1/stages/{stage}/health/pipelines` and `olf health` report every
signal of every pipeline whatever `lakehouse_code/alerts.yml` says, so a
condition the policy leaves silent, or a project with no policy, still shows
as `critical` or `warning` here. The policy decides what notifies; health
decides what is true. The alert evaluator reads the same signals through
`read_stage_pipelines`, passing the stage's policy to apply a rule's `grace`
and `freshness` thresholds; the routes pass none, so they keep the
cadence-derived signals described above.

`GET /api/v1/stages/{stage}/health/services` is the services half of the stage
health and nothing else: the `stage` and its `services` list, from the probes
alone, with no Dagster read and no quality integration. It answers within the
probes' own timeout even while Dagster hangs. The alert evaluator reads it for
the platform conditions `service_degraded` and `health_unreadable`
([alerts guide](../guides/operations/alerts.md#service_degraded)); its service
principal, `alert-evaluator`, is the only service principal the permission
matrix admits, and this is the only route it may call. People read it with the
same `data-engineer` role floor as the full stage health.

## Monitoring capabilities

`GET /api/v1/monitoring-capabilities` tells a client what its monitoring page
can show before it calls any route above. It lists the enabled stages on which
the caller's role may call at least one monitoring route, with those routes in
`operations`, and the stage's sources in `capabilities`. Any role may ask: an
`analyst` or `viewer` gets `stages: []`, a `data-engineer` gets every stage
with `list_logs` on `dev` only, and a `platform-admin` gets everything.

The answer is built from the API's settings and its permission matrix alone,
with no probe or read, so `available` means the source is configured, not that
it answers; the health routes say whether it does. Each entry carries a
`reason`, which names no host, URI or bucket.

`operations` includes the run reads `get_run` and `get_run_failure`, so a
client can tell whether a role may open a failed run before it calls them.
Each stage also carries `log_retention_days`: how many days its log archive
keeps a line, the provider contract's `logs.archive.retention_days` when its
`retention_supported` is true. It is `null` on a stage with no archive and on a
provider that applies no retention.

| Source | `available` when | Otherwise |
| --- | --- | --- |
| `dagster` | The stage has a Dagster GraphQL URL | `disabled` |
| `services` | The API has service probes and the stage a namespace | `disabled` |
| `quality` | The stage has a quality manifest prefix and its read-only key | `not_provided` |
| `quality-sync` | The stage has a quality-sync summary and the API has probes | `not_provided` without a summary, `unavailable` without probes |
| `logs` | The stage has a log archive and the API its read key | `not_provided` without an archive, `unavailable` without the key |
| `metrics` | The profile enables metrics and the provider gives a query URL | `disabled` when the profile turns them off, `not_provided` without a URL |
| `alerts` | The alert evaluator's state is configured and the API has its read key | `not_provided` |

## Rollup

`rollup` returns the worst state of signals from one stage. The order, worst
first, is `critical`, `warning`, `unknown`, `healthy`, `disabled`: an unknown
signal is never hidden by a healthy one, and a stopped pipeline does not turn
its product red. Signals never merge across stages: `rollup` raises on a mix,
and `rollup_by_stage` returns one state per stage.
