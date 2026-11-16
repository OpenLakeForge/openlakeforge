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
| `expected_run` | The schedule state and the run it expected |
| `freshness` | Whether a successful run is on record since the schedule's previous due time plus its grace |
| `quality` | A Floe or dbt outcome |
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
`execution` and `latest_success` to one cadence (the gap between the latest due
time and the one before it) plus the grace. A pipeline with no running schedule
keeps the provisional seven days (`RUN_MAX_AGE`, #1127).

## How freshness is computed

Freshness asks whether the pipeline kept its own cadence. It invents no
threshold: an SLA is #921 (v0.7.5), so the only bound is the schedule's cron.
`evaluate_freshness` takes the latest successful run, the due time whose run is
owed, `now`, the grace and the schedule state.

The platform API derives the owed due time from `latest_due` (croniter in the
schedule's timezone, never from Dagster ticks). Once `now` is at least the latest
due time plus the grace, that due time is owed; before then the due time before
it is, so a run still inside its grace cannot hide a missed earlier one.

| Observation | State |
| --- | --- |
| No schedule (a manual pipeline), or a stopped one | `disabled`, never stale |
| A run succeeded at or after the owed due time | `healthy` |
| No success since the owed due time, but `now` is within its grace | `healthy` |
| No success since the owed due time plus the grace | `warning` (stale) |
| No successful run in the runs read (the latest 25 per job) | `unknown` |
| The cron cannot be read, or nothing shows the schedule was on at the owed due time | `unknown` |

Stale is `warning`, not `critical`: a run that never started is already
`critical` through `expected_run`, and a failed run through `execution`. A
success counts by the time it ended. The signal carries no `expires_at`, since
it expires at the next due time, which the module does not know.

## How critical is computed

| Observation | State |
| --- | --- |
| Latest run failed | `critical` |
| Schedule running, a run was due at least the grace period ago, and no run started since | `critical` |
| Quality ERROR that blocks the pipeline | `critical` |
| A component with no ready pod, or with an unschedulable pod | `critical` |

A missed run is critical without any failure event, because a stopped
scheduler emits none. A schedule that is stopped or absent is `disabled`, not
`critical`. The last expected run time comes from the schedule input; the
module does not parse cron.

Other outcomes: a canceled run, a quality WARN or non-blocking ERROR, and a
component that is scaled to zero, partly ready, crash looping or restarting
repeatedly give `warning`; a run in progress and a passing check give `healthy`.

## Rollup

`rollup` returns the worst state of signals from one stage. The order, worst
first, is `critical`, `warning`, `unknown`, `healthy`, `disabled`: an unknown
signal is never hidden by a healthy one, and a stopped pipeline does not turn
its product red. Signals never merge across stages: `rollup` raises on a mix,
and `rollup_by_stage` returns one state per stage.
