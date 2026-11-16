# ADR 0023: Alert evaluation, incident state and webhook delivery

## Status

Decided, **not built**. Nothing in `infra/`, `tools/olf` or `packages/` changes
with this record. What binds today is the direction: a small evaluator that runs
as a CronJob in `olf-system`, state kept as objects in the ops bucket, and one
generic HTTPS webhook adapter. Slices #1168 to #1173 build it (see "Build
order"). Tags used below: **[V]** checked in this repository at the commit this
record was written against; **[T]** a claim the implementing slice must test
before relying on it.

Every decision carries a basis. **Maintainer** means the maintainer chose it on
2026-11-16 and it binds every slice of #23. **Default, revisit** means the issue
recorded it as a default the maintainer has not chosen explicitly; it binds the
build until the maintainer reads it here and changes it. **Proposed** means this
record had to fill a gap the issue left; it is listed again under "Open items".

## Context

[#23](https://github.com/OpenLakeForge/openlakeforge/issues/23) asks for
notifications when a run fails, a scheduled run never starts, output goes stale
or a quality check warns, without Prometheus, Loki, Grafana or a SaaS. Its
2026-11-16 comment fixes the scope: **one** generic HTTPS webhook adapter,
canonical health signals, and an evaluator for the absence of runs that works on
Slim. A missed run produces no event, so something has to look for it.

| Fact | Where it is verified |
| --- | --- |
| `spec.observability.alerts.receivers` exists: `name`, `type` (only `webhook`) and `secret_ref` (`name`, `key`) are required; an inline credential is an unknown field and is rejected | `_validate_alert_receiver` in `tools/olf/olf/common/profile.py`; [deployment profile](../reference/deployment-profile.md#specobservability) |
| The provider contract carries `shared.observability.alerts` as `{receivers, supported}`; all three environments set `alerts = false` and the reference says no component acts on it yet | `environments/{local,aws-poc,azure-poc}/contracts.tf`; `modules/contracts/provider-contract/outputs.tf`; [provider contracts](../architecture/provider-contracts.md) |
| The health vocabulary is canonical: `HealthState` (`healthy`, `warning`, `critical`, `unknown`, `disabled`), `SignalKind` (`execution`, `latest_success`, `expected_run`, `freshness`, `quality`, `service`) and the pure rules `evaluate_execution`, `evaluate_latest_success`, `evaluate_expected_run`, `evaluate_quality` | `packages/core/openlakeforge_core/health.py` |
| A failed run is `critical`, a canceled one `warning`; a due run that never started is `critical` ("a scheduler that is down emits no failure event"); a stopped or absent schedule is `disabled` | `evaluate_execution`, `evaluate_expected_run` in `health.py` |
| The API computes pipeline signals from Dagster reads and a due time derived from the cron alone, never from a tick; `GET /api/v1/stages/{stage}/health` and the pipelines route already return them. Each product pipeline's latest run also carries one `quality` signal read from its manifest with a per-stage read-only key; a stage without the key reports the quality integration as `not_provided` and adds none | `health_routes.py` (`_quality`), `pipeline_health.py` (`_evaluate`), `quality_health.py`, `schedules.py` (`latest_due`) in `packages/platform-api/openlakeforge_platform_api/`; `quality_reader_credentials` in `modules/storage/seaweedfs/credentials.tf` |
| That computation takes a `Request` today | `_read(request, ...)` in `health_routes.py`; #1161 removes the dependency |
| The expected-run grace is 15 minutes and a run is looked back over a window of 25 | `SCHEDULE_GRACE`, `RUN_WINDOW` in `health_routes.py` |
| A quality outcome is `passed`, `warn`, `error`, `not_run` or `unknown`; a per-run manifest is keyed `run-artifacts/quality/<domain>/<product>/<run>/manifest.json` under a stage's artifact prefix | `packages/core/openlakeforge_core/quality.py` (`QualityOutcome`, `manifest_key`); `run_artifact_base_uri` in `tools/olf/olf/common/provider_contracts/_model.py` |
| The platform API image runs from source on `PYTHONPATH`, as `nobody`, with a read-only root filesystem, and its `CMD` is `uvicorn`; the API itself is stateless | `images/platform-api/Dockerfile`; [ADR 0021](0021-platform-api.md), decision 3 |
| A pod can read and write the ops bucket only through a key the SeaweedFS credentials Job mints, scoped by prefix with `Read`, `Write`, `List` actions; `Write` also overwrites and deletes | `modules/storage/seaweedfs/credentials.tf` (`log_reader_credentials`, `audit_writer_credentials`); [ADR 0021](0021-platform-api.md), decision 3 |
| `audit/platform-api/` is already a bucket-root prefix of the ops bucket, beside `activations/<stage>/` | `platform_api_audit_prefix` in `infra/terraform/environments/local/main.tf` |
| CronJobs the platform applies use `concurrencyPolicy: Forbid`, one retained success and three retained failures, and get Secrets through `secretKeyRef` | `tools/olf/olf/infra/deployment/activation.py`; `tools/olf/olf/infra/openmetadata/_role_sync.py` |
| Stage services are `<service>-<stage>.<base_domain>`, so Dagster is `dagster-<stage>.<base_domain>` | [ADR 0013](0013-service-access-and-ingress.md), decision 2 |
| A requested retention is reported, not applied: no lifecycle rule reaches the log prefix | `logs_archive_retention` comment in `environments/local/contracts.tf`; [ADR 0022](0022-pod-log-collection-and-archive-format.md), decision 6 |
| `olf-system` is default-deny for ingress and egress, so every flow the evaluator needs is a row in the flow table | [ADR 0017](0017-local-network-policy-and-rbac.md); `release/network-flows.yaml` |

## Decision 1: a CronJob in `olf-system`, not a Dagster sensor

| Basis | Decision |
| --- | --- |
| Default, revisit | The evaluator runs every 5 minutes (`*/5 * * * *`). |
| Maintainer | It is a CronJob in `olf-system` that runs the platform API image with `python -m openlakeforge_platform_api.alerting` **[T: the module does not exist yet]**. The sensor is rejected. |

The image's `CMD` is `uvicorn`; the CronJob overrides the command, so no second
image is built or pinned. The evaluator imports the same rules the health routes
use (`openlakeforge_core.health`) and the same Dagster client
(`openlakeforge_services.dagster`) **[V: both are in the image build, `COPY`
lines of `images/platform-api/Dockerfile`]**. #1161 first makes the pipeline
read callable without a `Request`; until then the evaluator would have to
duplicate `_read`.

Why not a sensor, reasons this record gives:

1. **A sensor shares fate with what it watches.** Dagster's sensor daemon runs
   inside the stage's Dagster. The failure the evaluator exists for is a
   scheduler or daemon that stopped; the same outage stops the sensor, and
   `evaluate_expected_run` was written for that case.
2. **A sensor lives in user project code.** It would be deployed with each stage's
   code location, which is the artifacts phase. Receivers, state and delivery
   are platform-wide and belong to the platform phase; AGENTS.md rule 3 says a
   platform apply never waits on an artifact, and an alert path that depends on
   a code deploy fails that rule.
3. **A sensor sees one stage.** One evaluator reads every enabled stage's Dagster
   and writes one state, so DEV and PROD incidents are deduplicated in one place
   and a receiver's `stages` filter is applied once.
4. **The 5 minutes cost nothing.** A CronJob pod exists for seconds; there is no
   resident process and Slim gains no component.

The CronJob is rendered only when the profile declares at least one receiver
**(proposed)**. With none there is nothing to deliver, and a team without a
receiver pays no pod, no key and no flow rows. Portal and CLI warnings come from
the live health routes, not from incidents, so they do not depend on it.

`concurrencyPolicy: Forbid` is what makes the evaluator the single writer of its
state (Decision 3).

## Decision 2: which signals become incidents

The evaluator reads each enabled stage with the rules of
`openlakeforge_core.health` and maps conditions to incidents. It adds no
thresholds of its own: customer thresholds stay in Floe, dbt and project
configuration (#23).

| Condition | Opens when | Severity | Basis |
| --- | --- | --- | --- |
| `run_failed` | the latest run of a product's job failed (`execution` is `critical`) | `critical` | Default, revisit: critical signals alert |
| `missed_run` | `expected_run` is `critical`: a running schedule, a due time past its 15-minute grace, no run started since | `critical` | Default, revisit |
| `stale_output` | a running schedule's job has no success since the previous due time plus grace | `critical` | Default, revisit: stale is that rule, with no SLA input (SLAs are #921, v0.7.5) |
| `quality` | the latest quality outcome is `warn` or a non-blocking `error` (severity `warning`), or a blocking `error` (severity `critical`). One condition with two severities, so a warning that turns into a blocking error stays one incident (see Severity escalation) | `warning` or `critical` | Default, revisit: quality WARN alerts as `warning`; critical signals alert |
| `schedule_mismatch` | a PROD schedule the product's cadence declares running is stopped in Dagster (#1175, #209 slice c) | `warning` | From #1169; resolves silently when the declaration or the schedule changes |
| `stage_unreadable` | Dagster for a stage could not be read in 3 consecutive evaluations | `critical` | Default, revisit for the count of 3; the severity is proposed |

What does not alert, all **default, revisit**:

- A **canceled** run. `evaluate_execution` reads it as `warning`, and a person
  canceled it.
- A single `unknown` signal. `unknown` neither opens nor resolves an incident: a
  missing observation is not evidence in either direction
  (`health.py`, `worst_state`). Only three consecutive unreadable evaluations
  of a stage raise `stage_unreadable`, and while a stage is unreadable its open
  incidents are held, not resolved.
- A schedule that is `disabled`, stopped or absent. `evaluate_expected_run`
  returns `disabled` for it, and an intentionally stopped pipeline must not
  page anyone.
- Resource conditions (pod readiness, storage, Trino latency) and rejection-rate
  thresholds. There are no Prometheus-sourced alerts in v0.7, and AWS and Azure
  keep `alerts.supported = false` **[V: `environments/aws-poc/contracts.tf`,
  `environments/azure-poc/contracts.tf`]**. The local environment flips its
  `supported` to `true` in the slice that deploys the CronJob (#1172).
- `service` signals from the stage health route. They are not a v0.7 alert
  source **(proposed)**: the issue names run, schedule, freshness and quality
  conditions only.

`quality` conditions read the quality manifests that #1162 writes, through the
same `read_stage_pipelines` the health routes use (`quality_health.py`), so the
two cannot disagree. A pipeline whose run wrote no manifest reads `unknown` and
opens nothing. The evaluator takes the readers as an argument; mounting a key
for it is part of the CronJob (#1172).

`stale_output` is suppressed while `missed_run` or `run_failed` is open for the
same product **(proposed)**: either already says why the output is stale, and a
second notification for one cause is the duplicate #23 forbids. The `freshness`
rule itself belongs to #1174 (#209 slice b); this record fixes only what the
evaluator does with it.

## Decision 3: incident identity, state and deduplication

### Identity

An incident is identified by its **fingerprint**: an opaque hash over the model
version, stage, condition, domain, product, table and component
(`openlakeforge_core.alerts.fingerprint`, #1169). It excludes the run id, so
repeated failures of one product are one incident, and it includes the domain and
table, so two products with the same name in different domains never collide. A
recurrence after a resolve is a new incident with the same fingerprint, told apart
by an **id**: `<fingerprint>-<opened_at>-<seq>`, where `opened_at` is the UTC
epoch second the incident opened and `seq` is a per-fingerprint counter kept in
state. The counter keeps ids apart when a resolve and a re-open fall in the same
second (as under the e2e's fixed `--now`); the time keeps them apart when state
is lost and the counter restarts (see Missing or corrupt state).
Repeated evaluations of an open key update `last_seen` and the latest run id and
send nothing. A second failed run while `run_failed` is open is the same
incident: one notification per incident, not per run.

### Resolve rules

| Condition | Resolves when | Notifies |
| --- | --- | --- |
| `run_failed` | the latest run **succeeded**. A run in progress does not resolve it: `evaluate_execution` reads "in progress" as `healthy`, which would resolve the incident while a retry is still running | yes, once |
| `missed_run` | a run started at or after the due time | yes, once |
| `missed_run`, schedule stopped while open | the schedule becomes `disabled` | **no**; recorded in history as `resolved_silently`. Default, revisit |
| `stale_output` | a success at or after the due time | yes, once |
| `quality` | a later run's outcome is `passed` | yes, once |
| `stage_unreadable` | the first evaluation that reads the stage | yes, once |

Incidents notify **once on fire and once on resolve**, with no reminders
(Maintainer). There is no acknowledgement in v0.7 (Maintainer): no route, no
`acknowledged` state and no POST. Incident status may be readable, through the
platform API with a read-only key on `alerts/state/`, as the logs route is
**[T: not decided to be built here; #793 owns the route]**. If acknowledgement
is ever built, it is written as **separate objects** under `alerts/ack/`, which
the evaluator reads and never edits, so the evaluator stays the only writer of
state.

### Severity escalation

A `quality` incident whose outcome moves from a warning to a blocking error for the
same product keeps its key and id; its severity moves from `warning` to `critical`. The escalation sends
**one** notification (`event: escalated`) and no repeat reminders. A move from
`critical` to `warning` sends nothing; the history records it. Default, revisit.

### State in the ops bucket

All alert objects are under the bucket-root prefix `alerts/` of the ops bucket,
beside `audit/platform-api/` **[V]**, with one key minted for it:
`Read`, `Write` and `List` on `alerts/*`, and `Read` and `List` on each stage's
`activations/<stage>/run-artifacts/quality/*` for the manifests. It follows the
pattern of `log_reader_credentials` and `audit_writer_credentials` in
`credentials.tf` **[V]**. `Write` can delete and overwrite
(`credentials.tf`, ADR 0021), so the key is not append-only; the history is
tamper-evident against everyone but the evaluator, not immutable.

| Object | Content |
| --- | --- |
| `alerts/state/v1/state.json` | `schema_version`, `evaluated_at` (written by the evaluator), the open incidents only (fingerprint, id, severity, notified severity, opened_at, last_seen, last run), the per-fingerprint id sequence, the per-stage count of consecutive unreadable evaluations, and an **outbox** of notifications not yet delivered |
| `alerts/history/v1/date=<YYYY-MM-DD>/<incident-id>-<seq>-<event>.json` | one object per event (`opened`, `escalated`, `resolved`, `resolved_silently`) and per delivery attempt (receiver name, attempt number, HTTP status or error class, time). No payload body and no URL |
| `alerts/state/v1/corrupt/<UTC time>.json` | a state object that failed to parse, kept for the operator |

Event status and delivery status are separate: an incident can be `firing` with
its notification `pending`, `delivered` or `undeliverable`.

A run of the evaluator writes in this order, which is what makes delivery
asynchronous from evaluation:

1. Read the stage signals and the state.
2. Apply the rules, append the new events to the outbox and write
   `state.json`.
3. Deliver the outbox. Each attempt is recorded in history; a success removes the
   receiver from the entry; the entry leaves the outbox once every receiver routed to it has it, or once it expires, and `state.json` is rewritten.

A crash between a delivery and the rewrite sends the notification again with the
same idempotency key (Decision 4). The design is at-least-once, never at-most-once:
better a duplicate than silence.

If the bucket cannot be read or written, the evaluator **delivers nothing and
exits non-zero**. Delivering without being able to record it would repeat every
notification every 5 minutes. The Job's retained failures are the operator's
signal.

### Missing or corrupt state

A missing `state.json` is an empty state: the first run after a deploy opens an
incident for every condition that is firing and notifies each. A `state.json`
that does not parse is copied to `alerts/state/v1/corrupt/` first, never
overwritten, and treated the same. Either way **incidents currently firing are
notified again**: better a duplicate than silence. Default, revisit. Because
an id carries its opening time, a re-opened incident gets a new id and a new
`Idempotency-Key`, so a receiver that deduplicates on the key still shows it,
and later recurrences never collide with ids issued before the loss.

### Single writer

`concurrencyPolicy: Forbid` allows one evaluator at a time. SeaweedFS conditional
writes are not relied on **[T: whether `PutObject` honours `If-Match` on 4.48 is
untested, so the design does not need it]**. If two evaluators ever overlap
(a Job recreated after a node loss), the result is a duplicate notification, the
failure the design already accepts.

### The test flags

`--now <timestamp>` fixes the evaluation clock and `--state-prefix <prefix>`
points state and history at a different prefix. Both are for the e2e only
(Default, revisit: acceptable). They let #1173 inject a missed run by moving the
clock instead of waiting for a schedule, and keep the test's state out of the
real `alerts/`. The CronJob never passes them.

## Decision 4: the webhook contract

One adapter, `type: webhook`, in `packages/lakehouse-services` **[T]**. A later
Slack, email or managed adapter implements the same notifier interface, which is
the receiver `type` the profile already validates **[V: `_RECEIVER_TYPES` in
`profile.py`; `email` and `slack` are rejected today]**.

| Rule | Behaviour | Basis |
| --- | --- | --- |
| Credential | The URL stored under the receiver's `secret_ref` **is** the credential. There is no separate token, and no HMAC signing in v0.7 | Maintainer |
| Transport | HTTPS only. A non-`https` URL is refused before any request. Redirects are **not** followed: a 3xx is a rejected delivery, not retried. urllib3 logs the request path (the credential) at DEBUG, so the evaluator calls `notify.quiet_transport_logs()` at startup | Maintainer |
| Secret reference | The Secret lives in `olf-system`, is created out-of-band, and reaches the pod through `secretKeyRef`. The reference (name and key, never the value) may enter Terraform state. **Deploy fails with a clear message if the Secret or the key is missing** | Maintainer |
| URL handling | The URL is never logged, never written to state or history, and never sent in a payload. Logs and history name the receiver, not its host | Proposed |
| Method and body | `POST`, `Content-Type: application/json`, the payload below | Proposed |
| Idempotency | `Idempotency-Key: <incident id>:<event>`, identical on every retry of one notification | Proposed |
| Timeouts | 5 seconds to connect and 10 seconds to read, per attempt (`openlakeforge_services/notify.py`, #1170) | Proposed |
| Retries | At most 3 attempts per evaluator run. Between attempts the notifier waits an exponential backoff with jitter (0.5 s base, 4 s cap), or the receiver's `Retry-After` when longer, capped at 10 s, so one `send` is bounded (`notify.MAX_TOTAL_SECONDS`). Connection errors, 408, 429 and 5xx are retried. A notification still failing stays in the outbox and is retried by the next runs; after 12 runs (about an hour) it is marked `undeliverable` in history and dropped from the outbox. 2xx is success; 4xx other than 408 and 429 is not retried | Proposed |
| Stage routing | A receiver's optional `stages` list (for example `[prod]`) limits it to those stages; omitted means all stages. A stage the profile does not enable is a validation error | Maintainer; built in #1168 |

**Why the deploy check cannot be a Terraform data source.** A
`kubernetes_secret_v1` data source reads the value and writes it to state, which
the decision forbids. The check is a precondition that asks the cluster whether
the Secret and key exist without returning the value **[T: `olf` performs it
before the platform apply, as the role-sync CronJob checks its Secret before
rendering (`apply_role_sync` in `_role_sync.py`)]**.

### Payload allowlist

The body is built from a closed list of fields. Nothing else is copied from a
signal, a run or a log.

| Field | Content |
| --- | --- |
| `schema` | `openlakeforge.alert.v1` |
| `event` | `opened`, `escalated` or `resolved` |
| `incident_id`, `condition`, `severity` | as Decision 3 |
| `stage`, `domain`, `product`, `table` | as the signal's scope; absent fields are omitted |
| `run_id` | the Dagster run id, when the condition has one |
| `opened_at`, `event_at` | UTC timestamps |
| `summary` | the rule's fixed `reason` string (`health.py` builds them from constants, such as "latest run failed") |
| `links` | Decision 5 |

Never in a payload: log lines, run tags, error or exception text, row contents,
sample values, storage URLs, receiver URLs or credentials. The quality record
already refuses those keys on the way in
(`_FORBIDDEN_KEYS` in `quality.py` **[V]**), and the health routes answer with
"run ids, statuses and times only" (`health_routes.py` docstring **[V]**); the
payload keeps to the same line. A test serialises every condition's payload and
asserts that its keys are a subset of this table **[T]**.

## Decision 5: deep links

| Link | Target | Until then |
| --- | --- | --- |
| `run` | `<scheme>://dagster-<stage>.<base_domain>/runs/<run_id>` for a condition with a run, where `<scheme>` is `http` when `spec.access.tls` is `none` (the local default, ADR 0013 decision 9) and `https` for every TLS mode. The evaluator takes the scheme from the access contract, never a constant. The host pattern is ADR 0013's **[V]**; the `/runs/<id>` path is Dagster's UI route **[T: checked against the pinned Dagster version in #1173]** | Always available once the route exists |
| `portal` | the Portal's diagnostic page for the incident (#333) | Omitted until #333 exists |
| `runbook` | an anchor in the published docs for the condition (`stale_output`, `missed_run`, ...) | A docs anchor until runbooks (#51) exist (Default, revisit) |

The Dagster link goes through the stage's perimeter, so the receiver's reader
must sign in and hold a role for that stage (ADR 0013, ADR 0014); a notification
leaks the existence of a run id and a host, not its logs or data. The link is
never a privileged log URL, and the evaluator never mints a token for it. The
`base_domain` is the profile's `spec.access.base_domain`; a deployment without
`spec.access` has no ingress and the `run` link is omitted **[T]**.

## Decision 6: the outside-in limit

An evaluator that runs inside the cluster cannot report that the cluster is gone.
A full-cluster outage, a node pool that cannot schedule `olf-system`, a stopped
CronJob controller or an unreachable object store all produce **no alert**; the
last case at least leaves failed Jobs (Decision 3). This record states the limit
and does not work around it.

The later option is an external heartbeat: the evaluator makes one request to an
operator-owned URL after each successful evaluation, and an outside service
alerts when the pings stop. Its URL would be another receiver-style Secret
reference. It is **not built** and not part of #23's v0.7 scope.

`state.json` carries `evaluated_at`, so a reader can already tell a stale
evaluator from a healthy one.

## Decision 7: network and identity

The evaluator is a new client, so it adds flow rows to
`release/network-flows.yaml` and to ADR 0017 in the slice that deploys it
(#1172). Each follows an existing row:

| Flow | Follows | Note |
| --- | --- | --- |
| evaluator to each stage's Dagster webserver, port 80 | F32 | read-only GraphQL, as the API's reads |
| evaluator to SeaweedFS S3, port 8333 | F33 | with its own key (Decision 3) |
| evaluator to the receivers, port 443, any address outside the pod and Service ranges | F28, F30 | NetworkPolicy has no FQDN selector, so the rule is as wide as those two; the receiver's URL is the only thing that narrows it |

The e2e's receiver is a stub pod in `olf-system` carrying a label the flow file
names, as the Dex e2e's GitHub stub does (F29 **[V]**) **[T]**. Its listener is
HTTPS, because the adapter refuses plain HTTP.

The evaluator's ServiceAccount has no Kubernetes API access: it needs none while
service signals are not alerted.

## Consequences

- The platform gains one CronJob and one object-store key, and only on a profile
  with receivers. Slim gains no resident component and no Prometheus
  dependency.
- The API stays stateless (ADR 0021, decision 3). The image hosts a second
  entrypoint, so the API and the evaluator are released together and cannot
  drift on the health rules.
- Alerting is at-least-once. A receiver must deduplicate on `Idempotency-Key`
  or tolerate an occasional repeat.
- The webhook URL is a bearer credential. Whoever can read the Secret in
  `olf-system` can send as the platform; rotation is replacing the Secret.
- `alerts.supported` stays `false` on AWS and Azure; they keep the receivers
  field in the contract and nothing acts on it.
- 5 minutes is the floor on detection latency, and a missed run is reported no
  earlier than the due time plus the 15-minute grace.

## Retention

`alerts/history/` is kept for **30 days**; `alerts/state/` is never expired. Like
ADR 0022, decision 6, retention is a lifecycle rule on the prefix through the ops
bucket contract, and whether SeaweedFS 4.48 honours bucket lifecycle rules is
**[T]**: no lifecycle rule reaches any prefix today **[V: the
`logs_archive_retention` comment in `environments/local/contracts.tf`]**. Until
a test shows it does, the 30 days is a request, not a guarantee, and the
evaluator does not delete history itself.

## Open items

Things this record had to decide because the issue did not. Each is marked
Proposed above; the maintainer may change any of them.

1. The CronJob exists only with at least one receiver.
2. `stage_unreadable` is `critical`.
3. `stale_output` is suppressed while `missed_run` or `run_failed` is open for
   the same product.
4. `service` signals are not an alert source in v0.7.
5. The webhook's timeouts, attempt counts, backoff, outbox lifetime of 12 runs and
   `Idempotency-Key` format.
6. The payload field list, and the URL never appearing in logs or history.
7. The deploy-time Secret check is an `olf` precondition, not a Terraform data
   source, so no value enters state.

## Build order

| Slice | Issue | Content | Updates |
| --- | --- | --- | --- |
| P | #1161 | The pipeline read without a `Request` | `health_routes.py` |
| Q-a | #1162 | The per-run quality manifest the quality conditions read | `packages/runtime` |
| 23-p | #1168 | `stages` on a receiver: schema, validator, resolved output | `profile.py`, `docs/schema/deployment-profile.schema.json`, `docs/reference/deployment-profile.md` |
| 23-b | #1169 | The incident model, deduplication, resolve and escalation rules, pure, in `packages/core` | tests |
| 23-c | #1170 | The webhook notifier with bounded retries | `packages/lakehouse-services` |
| 23-d | #1171 | The `alerting` entrypoint and the ops-bucket state | `packages/platform-api` |
| 23-e | #1172 | The CronJob, the key, the flow rows, the Secret check, `alerts.supported = true` locally | ADR 0017 flow table, ADR 0021 |
| 23-f | #1173 | E2e: failed, missed, stale and quality incidents into a stub; the alerts documentation and runbook anchors | `docs/reference/`, `docs/technical-debt.md` |

## Rejected

| Option | Why not |
| --- | --- |
| A Dagster sensor | Decision 1: it shares fate with the scheduler it watches and lives in user project code |
| Prometheus Alertmanager | Needs #21, which Slim does not run; the alerts of #23 must work without it |
| Acknowledgement in v0.7 | Maintainer: no route, no reminders, so nothing needs an ack |
| Reminders while an incident stays open | Maintainer: one notification on fire, one on resolve |
| HMAC-signed bodies | Maintainer: the URL is the credential in v0.7 |
| Following redirects | Maintainer: a redirect can leave HTTPS or the receiver the operator chose |
| State in PostgreSQL | The API is stateless until a feature needs storage (ADR 0021); a bucket object is enough for one writer and costs no schema |
| Several adapters at once | #23: one webhook first, a provider-neutral interface for the rest |

## History

2026-11-16: created for #1167 (slice 23-a of #23).

2026-11-16: quality signals are read from the per-run manifests (#1186, slice
c of #793); the health routes report the quality integration as `available` or
`not_provided`.

2026-11-16: only a successful run's manifest can read quality `healthy`. Runs
are read newest first; one that did not succeed (in progress, canceled, or
failed before dbt reported) answers only with a `warning` or `critical`, so a
retry's partial manifest cannot resolve the previous run's quality incident,
even when an older success precedes the failure.
