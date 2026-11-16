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
| `quality` | A Floe or dbt outcome |
| `freshness`, `service` | In the vocabulary; an evaluator for each comes with its consumer |

## How unknown is computed

An observation is usable for `max_age` after `observed_at`. A missing
observation, or one at least `max_age` old, gives `unknown` for every kind,
whatever the old observation said. A failed fetch is therefore never green.
`max_age` has no default; the caller sets it from how often it observes.

## How critical is computed

| Observation | State |
| --- | --- |
| Latest run failed | `critical` |
| Schedule running, a run was due more than the grace period ago, and no run started since | `critical` |
| Quality ERROR that blocks the pipeline | `critical` |

A missed run is critical without any failure event, because a stopped
scheduler emits none. A schedule that is stopped or absent is `disabled`, not
`critical`. The last expected run time comes from the schedule input; the
module does not parse cron.

Other outcomes: a canceled run and a quality WARN or non-blocking ERROR give
`warning`; a run in progress and a passing check give `healthy`.

## Rollup

`rollup` returns the worst state of signals from one stage. The order, worst
first, is `critical`, `warning`, `unknown`, `healthy`, `disabled`: an unknown
signal is never hidden by a healthy one, and a stopped pipeline does not turn
its product red. Signals never merge across stages: `rollup` raises on a mix,
and `rollup_by_stage` returns one state per stage.
