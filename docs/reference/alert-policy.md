# Alert policy reference

Every key of `lakehouse_code/alerts.yml`, the alert policy that declares which
conditions alert for which products and stages, with which options, and through
which routes. It matches
[`alert-policy.schema.json`](../schema/alert-policy.schema.json) and the parser
in `openlakeforge_core/alert_policy.py`; the guide to writing one is
[send alerts to a webhook](../guides/operations/alerts.md#write-the-alert-policy).

The file is optional. Without it nothing alerts for any product; only the
platform conditions do. Unknown keys are rejected at every level.

## Top level

| Key | Type | Required | Allowed values |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | `openlakeforge.io/v1beta1` |
| `kind` | string | yes | `AlertPolicy` |
| `routes` | map of route name to receiver name | yes | At least one entry; see [Names](#names). |
| `defaults` | object | no | `stages`, `notify` |
| `rules` | list of rules | yes | At least one rule. |

### `routes`

Each key is a route name and each value the `name` of a receiver in
`spec.observability.alerts.receivers` of `openlakeforge.yaml`. Rules refer to
routes, so a receiver can be renamed in one place. The receiver itself, its
type and its Secret are declared only in the
[deployment profile](deployment-profile.md#specobservability).

### `defaults`

| Key | Type | Default | Allowed values |
| --- | --- | --- | --- |
| `defaults.stages` | list of stages | every stage | `dev`, `uat`, `prod`; non-empty, no repeats. |
| `defaults.notify` | list of route names | none | Keys of `routes`; non-empty, no repeats. |

Each applies to a rule that does not set its own.

## `rules[]`

| Key | Type | Required | Default | Allowed values |
| --- | --- | --- | --- | --- |
| `match.domain` | string | yes | | A domain `name` of `governance.yml`, or `"*"`. |
| `match.product` | string | yes | | A product `id` of `governance.yml`, or `"*"`. |
| `stages` | list of stages | no | `defaults.stages` | `dev`, `uat`, `prod`; non-empty, no repeats. |
| `notify` | list of route names | no | `defaults.notify` | Keys of `routes`; non-empty, no repeats. |
| `when` | map of condition to options or `false` | yes | | At least one [condition](#when). |

A rule that resolves to no route, neither its own `notify` nor
`defaults.notify`, is an error. A product named in `match.product` must belong
to the domain named in `match.domain`, unless the domain is `"*"`.

A job that belongs to no product, such as `gold_maintenance`, is matched only by
a rule whose `domain` and `product` are both `"*"`.

## `when`

Each key is a condition. Its value is an object of options, `{}` for the
defaults, or `false` to turn the condition off where a less specific rule turns
it on. `true`, `null` and any other value are rejected.

| Condition | Alert it opens | Option | Type | Default | Allowed values |
| --- | --- | --- | --- | --- | --- |
| `run_failed` | `run_failed` | `severity` | string | `critical` | `warning`, `critical` |
| `missed_run` | `missed_run` | `grace` | duration | `15m` | A [duration](#durations): how long past a due time before the run counts as missed. |
| | | `severity` | string | `critical` | `warning`, `critical` |
| `freshness` | `stale_output` | `warn_after` | duration | none | A [duration](#durations): the age of the last success that opens a `warning`. |
| | | `critical_after` | duration | none | A [duration](#durations): the age of the last success that opens a `critical`. |
| | | `grace` | duration | `15m` | A [duration](#durations); cadence-derived staleness only. |
| | | `severity` | string | `critical` | `warning`, `critical`; cadence-derived staleness only. |
| `quality` | `quality` | `level` | string | `warn` | `warn`, `error` |
| `schedule_mismatch` | `schedule_mismatch` | `severity` | string | `warning` | `warning`, `critical` |

`freshness` has two modes:

- **Thresholds.** `warn_after`, `critical_after` or both. Each measures the age
  of the last successful run and applies whatever the schedule's state. When
  both are set, `warn_after` must be below `critical_after`.
- **Cadence-derived.** Neither threshold. A running schedule's job has no
  success since the previous due time plus `grace`, at `severity`.

`warn_after` and `critical_after` do not mix with `grace` or `severity` in the
same rule; the schema and the parser both reject it.

`quality` with `level: warn` opens at `warning` for a Floe warning or a
non-blocking error and at `critical` for a blocking error. With
`level: error`, a warning alone does not alert. An `on` key is rejected with a
message that names `level`.

The platform conditions `stage_unreadable`, `policy_unreadable`,
`service_degraded` and `health_unreadable` are not `when` keys. The profile's
`platform_receivers` routes them.

## Names

| Value | Pattern |
| --- | --- |
| Route name, receiver name | `^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$`: lowercase letters, digits and hyphens, starting with a letter. |
| `match.domain`, `match.product` | `*`, or `^[a-z][a-z0-9_]*$`: a `governance.yml` identifier. |

YAML 1.1 reads unquoted `on`, `off`, `yes`, `no`, `y` and `n` as booleans, so
quote a route or receiver with such a name. The parser says so when it meets one.

### Durations

A positive integer followed by `m`, `h` or `d`, matching `^[1-9][0-9]*[mhd]$`:
`30m`, `26h`, `2d`. `0`, `1.5h`, `90s` and `1w` are rejected.

## Precedence

For each stage and condition, the matching rule that names a product wins over
one that names a domain with `product: "*"`, which wins over the global
`"*"`/`"*"`. The choice is per condition and takes the winner's `notify`, not
the union of the matching rules. A rule names a product whether or not it also
names the domain. Two rules of the same specificity that set the same condition
for an overlapping stage are an error, never resolved by file order.

`olf alert explain --stage <stage> --product <id>` prints the result for one
product, and `--job <name>` in place of `--product` for one job: a job of no
product gets the global rules alone, and `<product>_pipeline` resolves as that
product. It exits `0` when the policy was read (including when there is no
`alerts.yml`), `1` when the project, `governance.yml`, `alerts.yml` or the
product is invalid, and `2` for a stage other than `dev`, `uat` or `prod`, or
unless exactly one of `--product` and `--job` is given.

## What is checked where

| Check | Where |
| --- | --- |
| Keys, types, patterns and enumerations; `freshness` thresholds not mixed with `grace` or `severity` | `olf check contracts` against the schema |
| `warn_after` below `critical_after`; every `notify` and `defaults.notify` names a `routes` key; every rule resolves to a route; `match` names a domain and product of `governance.yml`, and a product under its domain; no same-level overlap | `olf check contracts`, `olf alert explain` and the evaluator |
| That each route's receiver is declared in the profile | Activating a revision, when the profile declares receivers, and the evaluator, which raises `policy_unreadable` for a receiver the profile lost |
| Stage names | Against `dev`, `uat` and `prod`, not against the profile: a rule for a stage the profile does not enable is inert |

## How the evaluator reads the policy

`alerts.yml` is an entry of the project revision's `descriptors` component, so
each stage is judged by the policy of the revision it runs
([ADR 0023](../adr/0023-alert-evaluation-and-webhook-delivery.md), D10). Per
stage and evaluation:

1. `activations/<stage>/ACTIVE.json`. When its activation revision equals the
   one cached for the stage, the cached policy is used and the read stops here.
2. `activations/<stage>/revisions/sha256/<digest>/ACTIVATION.json`, for the
   project revision.
3. `project/revisions/sha256/<digest>/PROJECT-REVISION.json`, then
   `descriptors/lakehouse_code/alerts.yml` and
   `descriptors/lakehouse_code/governance.yml` under the same prefix, each
   checked against the sidecar's sha256.

The cache, `alerts/state/v1/policy/<stage>.json`, holds the activation
revision, the `alerts.yml` document and the product ids of `governance.yml`;
the job map is derived from them on each read, so a newer evaluator
re-validates the cache and discards one it can no longer parse.

| Situation | Effect |
| --- | --- |
| No activation, or a revision without `alerts.yml` | An empty policy: no product alerts, no warning. The revision's `governance.yml` is still read when it has one. |
| A read fails, a digest does not match, or the file fails validation | The last good policy is used and `policy_unreadable` opens at `warning` for the stage, to the platform receivers. It resolves on the first clean read. |
| A route names a receiver the profile no longer declares | The policy is still used and cached; that route is skipped and `policy_unreadable` opens. |
| No last good policy and the policy cannot be read | The stage's open product incidents are held, nothing new opens, and its queued product notifications stay queued until they expire. |

The evaluator's key reads `activations/<stage>/ACTIVE.json*` and
`activations/<stage>/revisions/*` for each enabled stage, and
`project/revisions/*`; no `List` and no `Write`. The pointer's grant ends in
`*` because SeaweedFS denies an action that names an exact object. The
`project/revisions/*` grant reaches every frozen file of every revision; it
narrows to `project/revisions/sha256/*/descriptors/*` only if SeaweedFS is
shown to honour a wildcard in the middle of an action path, which is untested.
