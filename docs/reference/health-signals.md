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

| Kind | Computed from |
| --- | --- |
| `execution` | Status of the latest run |
| `latest_success` | Whether a successful run is on record |
| `expected_run` | The schedule state, the run it expected, and whether it is stopped against its declared cadence |
| `freshness` | Whether a successful run is on record since the schedule's owed due time (the latest one at least a grace old) |
| `quality` | A Floe or dbt outcome, read from the latest run's quality manifest (see below) |
| `service` | Controller readiness of a component, or a probe's answer |

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
| No successful run in the runs read (the latest 25 per job) | `unknown` |
| The cron cannot be read, or nothing shows the schedule was on at the owed due time | `unknown` |

Stale is `critical`, as ADR 0023 has `stale_output` alert as critical: output
that has not refreshed since its owed due time is a broken promise to its
consumers. A stale pipeline whose run also failed, or never started, is already
`critical` through `execution` or `expected_run`; the stage rollup takes the
worst signal, so that adds no new state. Alerting on these signals is planned,
not built: the design (ADR 0023, #23) has the evaluator suppress a stale-output
alert while a failed-run or missed-run alert is open for the same product, so one
cause would notify once. A success counts by the time it ended, and the latest
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
which every Floe and dbt step of the run rewrites. The domain is the run's
`openlakeforge/silver-domain` tag and the product is the job name without its
`_pipeline` suffix; a job with no such tag, a maintenance job for one, gets no
quality signal.

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
manifest and the walk stops there, except that a newer run whose manifest
exists but cannot be read (store error, malformed, foreign) may be hiding a
failure, so a success after it reads `unknown` rather than `healthy`; a run with
no manifest hides nothing. When none is reached the pipeline
carries no `quality` signal, which leaves an open incident open. A manifest
whose `results` are malformed (an entry that is not an object, wrong field
types) reads `unknown` like an unreadable one.

The worst check decides, and no table name is used, so a manifest written from
the baked dbt manifest, which names no table, reads the same as any other. A
stage without the reader adds no signal and reports the `quality` integration
as `not_provided`; with it the integration is `available`. The reader's key is
`Read` and `List` on that stage's `run-artifacts/quality/` prefix only
(`quality_reader_credentials` in `modules/storage/seaweedfs`).

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

## Rollup

`rollup` returns the worst state of signals from one stage. The order, worst
first, is `critical`, `warning`, `unknown`, `healthy`, `disabled`: an unknown
signal is never hidden by a healthy one, and a stopped pipeline does not turn
its product red. Signals never merge across stages: `rollup` raises on a mix,
and `rollup_by_stage` returns one state per stage.
