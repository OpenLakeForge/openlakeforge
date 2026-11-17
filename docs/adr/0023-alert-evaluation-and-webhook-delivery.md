# ADR 0023: Alert evaluation, alert policy, incident state and webhook delivery

## Status

Partly built. The evaluator, its incident state and the webhook adapter are
built on the local provider by slices #1168 to #1173 (see "Build order"): a
CronJob in `olf-system`, state kept as objects in the ops bucket, and one
generic HTTPS webhook adapter. Local reports `alerts.supported = true`; AWS and
Azure keep `false`. Not built: the history retention rule (see "Retention"),
and the last slices of the alert policy (AP-c's routing, then AP-d).

The alert policy is decided: a project's `lakehouse_code/alerts.yml` declares
which conditions alert per product and stage, with which thresholds, and who
is told (Decisions 2 and 8 to 11). The evaluator applies the declared
conditions, severities and thresholds, and is silent for everything else. Until
the routing slice of AP-c lands, a notification still goes to every receiver
whose `stages` include the incident's stage, whatever route the rule names.

Tags used below: **[V]** checked in this repository at the commit the claim
was written against, or in the pinned upstream package named beside it;
**[T]** a claim the implementing slice must test, or an upstream claim not
verified here, before anyone relies on it. A [T] whose slice has landed is
answered in History, not edited in place.

Every decision carries a basis. **Maintainer** means the maintainer chose it
(2026-11-16 for the evaluator, 2026-11-17 for the alert policy) and it binds
every slice. **Default, revisit** means #23 recorded it as a default the
maintainer has not chosen explicitly; it binds the build until the maintainer
changes it here. **Proposed** means this record had to fill a gap; the
evaluator's are listed under "Open items", and the alert policy's (the
`governance.yml` read, `olf alert explain`, not using Dagster's alerting) bind
the build until review changes them. **Proposed, accepted by default
2026-11-17 (maintainer may revisit)** means an alert-policy question put to
the maintainer whose recommended answer was accepted unchanged; see "Questions
answered on 2026-11-17".

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

The table above was verified on 2026-11-16, before the evaluator existed. Once
it was built, its rules were fixed: the conditions and their severities were
code, the grace a constant and the only routing a receiver's `stages` filter.
A product team could not say, per product and per stage, what to watch, when it
fires or who is told. The alert policy (Decisions 8 to 11) answers that; it
builds on these facts, checked at `373dbc7` on 2026-11-17:

| Fact | Where it is verified |
| --- | --- |
| The conditions and severities are code: `AlertKind` (`run_failed`, `missed_run`, `schedule_mismatch`, `stale_output`, `quality`, `stage_unreadable`) and `_severity_of` | `packages/core/openlakeforge_core/alerts.py` **[V]** |
| The missed-run grace and the evidence bound are constants: `SCHEDULE_GRACE` (15 minutes) and `RUN_MAX_AGE` (7 days) | `packages/platform-api/openlakeforge_platform_api/pipeline_health.py` **[V]** |
| Routing is each receiver's `stages` filter: a notification goes to every receiver whose `stages` include its stage | `ReceiverSetting.routes` in `alerting/settings.py`; `run_once` in `alerting/run.py` **[V]** |
| An incident's scope carries the Dagster job as `product` (`order_revenue_pipeline`), and no domain | `_evaluate` in `pipeline_health.py`; `_read_stages` in `alerting/run.py` **[V]** |
| A product's job is `<id>_pipeline`; `gold_maintenance` and `silver_maintenance` are jobs of no product | `Product.job_name` in `openlakeforge_core/inventory.py`; `product_dagster.py` **[V]** |
| Only PROD carries schedules; DEV and UAT are manual, because one revision serves every stage | `_SCHEDULED_STAGES` in `packages/runtime/openlakeforge_runtime/product_dagster.py` **[V]** |
| One `ProjectRevision` serves every stage; its `descriptors` component freezes `governance.yml` and each `source.yaml`, published under `project/revisions/sha256/<digest>/` in the ops bucket | `_descriptors_component`, `REVISION_PREFIX`, `publish` in `tools/olf/olf/infra/project_revision.py`; [ADR 0012](0012-project-revisions-and-promotion.md) **[V]** |
| A stage's active revision is found through `activations/<stage>/ACTIVE.json`, which names an `ACTIVATION.json` under `activations/<stage>/revisions/sha256/<digest>/`, which names the project revision | `active_key`, `activation_key`, `active` in `tools/olf/olf/infra/project_activation.py` **[V]** |
| Those readers live in `tools/olf` only; the platform API image carries `openlakeforge_core`, `openlakeforge_services` and `openlakeforge_platform_api` | `COPY` lines of `images/platform-api/Dockerfile` **[V]** |
| `olf init` copies the distribution's `lakehouse_code/` as the new project's template | `initialization.py` in `tools/olf/olf/common/` **[V]** |
| The alert e2e makes its run, missed-run and stale incidents on `gold_maintenance`, and its quality incidents on a product job | `tools/olf/olf/infra/e2e/_alerts.py` **[V]** |

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
   and routing is applied once.
4. **The 5 minutes cost nothing.** A CronJob pod exists for seconds; there is no
   resident process and Slim gains no component.

The CronJob is rendered only when the profile declares at least one receiver
**(proposed)**. With none there is nothing to deliver, and a team without a
receiver pays no pod, no key and no flow rows. Portal and CLI warnings come from
the live health routes, not from incidents, so they do not depend on it.

`concurrencyPolicy: Forbid` is what makes the evaluator the single writer of its
state (Decision 3).

## Decision 2: which conditions alert

The evaluator reads each enabled stage with the rules of
`openlakeforge_core.health` and maps conditions to incidents. A product
condition alerts only where the project's alert policy (Decision 8) declares it
for that product and stage.

| Basis | Decision |
| --- | --- |
| Maintainer | **Silent unless declared.** A product or stage that no rule matches does not alert, and a project without `alerts.yml` gets no product alerts. |
| Maintainer | A freshness rule may carry `warn_after` and `critical_after`, measured from the last successful run; without them, cadence-derived staleness applies. Full SLAs (business calendars, partitions) stay in #921. |

| Condition | Opens when | Default severity | Options (Decision 8) | Basis of the default |
| --- | --- | --- | --- | --- |
| `run_failed` | the latest run of a product's job failed (`execution` is `critical`) | `critical` | `severity` | Default, revisit: critical signals alert |
| `missed_run` | `expected_run` is `critical`: a running schedule, a due time past its grace (default 15 minutes), no run started since | `critical` | `grace`, `severity` | Default, revisit |
| `stale_output` | cadence-derived: a running schedule's job has no success since the previous due time plus grace. With thresholds: the last success is older than `warn_after` (`warning`) or `critical_after` (`critical`) | `critical` | declared as `freshness`: `warn_after`, `critical_after`, or `grace` and `severity` | Default, revisit for the cadence rule; thresholds are Maintainer |
| `quality` | the latest quality outcome is `warn` or a non-blocking `error` (severity `warning`), or a blocking `error` (severity `critical`). One condition with two severities, so a warning that turns into a blocking error stays one incident (see Severity escalation) | `warning` or `critical` | `level`: `warn` or `error` | Default, revisit: quality WARN alerts as `warning`; critical signals alert |
| `schedule_mismatch` | a PROD schedule the product's cadence declares running is stopped in Dagster (#1175, #209 slice c) | `warning` | `severity` | From #1169; resolves silently when the declaration or the schedule changes |
| `stage_unreadable` | Dagster for a stage could not be read in 3 consecutive evaluations | `critical` | none: a platform condition (Decision 11) | Default, revisit for the count of 3; the severity is proposed |
| `policy_unreadable` | the stage's alert policy cannot be used (Decision 10) | `warning` | none: a platform condition (Decision 11) | Maintainer |

What does not alert, all **default, revisit**:

- A **canceled** run. `evaluate_execution` reads it as `warning`, and a person
  canceled it.
- A single `unknown` signal. `unknown` neither opens nor resolves an incident: a
  missing observation is not evidence in either direction
  (`health.py`, `worst_state`). Only three consecutive unreadable evaluations
  of a stage raise `stage_unreadable`, and while a stage is unreadable its open
  incidents are held, not resolved.
- A schedule that is `disabled`, stopped or absent, for `missed_run` and the
  cadence-derived `stale_output`. `evaluate_expected_run` returns `disabled` for
  it, and an intentionally stopped pipeline must not page anyone. Freshness
  thresholds apply in any schedule state **(proposed, accepted by default
  2026-11-17; maintainer may revisit)**: DEV and UAT have no schedule, and a
  declared bound is the team's statement that the job should have run.
- Resource conditions (pod readiness, storage, Trino latency) and rejection-rate
  thresholds. There are no Prometheus-sourced alerts in v0.7, and AWS and Azure
  keep `alerts.supported = false` **[V: `environments/aws-poc/contracts.tf`,
  `environments/azure-poc/contracts.tf`]**. The local environment flips its
  `supported` to `true` in the slice that deploys the CronJob (#1172).
- `service` signals from the stage health route, the log archive's delivery
  signals among them. They are not a v0.7 alert source **(proposed)**: the issue
  names run, schedule, freshness and quality conditions only.

`quality` conditions read the quality manifests that #1162 writes, through the
same `read_stage_pipelines` the health routes use (`quality_health.py`), so the
two cannot disagree. A pipeline whose run wrote no manifest reads `unknown` and
opens nothing. The evaluator takes the readers as an argument; the CronJob
builds one per stage from `quality_manifests` in its settings, on its own key
(decision 3), which already holds Read and List on each stage's quality prefix.

`stale_output` is suppressed while `missed_run` or `run_failed` is open for the
same product **(proposed)**: either already says why the output is stale, and a
second notification for one cause is the duplicate #23 forbids. A job with no
success among the 25 runs read stays `unknown` and opens nothing, thresholds or
not.

### Moving to silent unless declared

Before the policy, every product's conditions alerted. The migration lands with
slice AP-c:

1. The sample project (`lakehouse_code/`, which `olf init` copies) ships an
   `alerts.yml`: the global rule for every stage with `run_failed` and
   `quality`, and for PROD with `missed_run`, `freshness` and
   `schedule_mismatch`, all routed through `on-call: on-call`.
2. The conformance profile's receiver is renamed from `e2e-receiver` to
   `on-call`, keeping its Secret, so the sample's route resolves. The e2e
   recognises its stub by the Secret (`RECEIVER_REF_NAME` in `_alerts.py`
   **[V]**), not the receiver name.
3. The alerts guide opens with a warning callout: without `alerts.yml`, a
   project gets only platform alerts. The release notes say the same.
4. On the first evaluation after an upgrade, a firing product incident that no
   rule now covers resolves silently (Decision 3), so an upgrade does not send a
   burst of resolves.

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

The alert policy is not part of the fingerprint, and the scope keeps the job as
`product`, so editing a rule never turns one incident into two.

### Resolve rules

| Condition | Resolves when | Notifies |
| --- | --- | --- |
| `run_failed` | the latest run **succeeded**. A run in progress does not resolve it: `evaluate_execution` reads "in progress" as `healthy`, which would resolve the incident while a retry is still running | yes, once |
| `missed_run` | a run started at or after the due time | yes, once |
| `missed_run`, schedule stopped while open | the schedule becomes `disabled` | **no**; recorded in history as `resolved_silently`. Default, revisit |
| `stale_output` | a success at or after the due time, or, with thresholds, a success younger than `warn_after` | yes, once |
| `quality` | a later run's outcome is `passed` | yes, once |
| `stage_unreadable` | the first evaluation that reads the stage | yes, once |
| `policy_unreadable` | the first evaluation that reads the stage's policy cleanly | yes, once |

Incidents notify **once on fire and once on resolve**, with no reminders
(Maintainer). There is no acknowledgement in v0.7 (Maintainer): no route, no
`acknowledged` state and no POST. Incident status may be readable, through the
platform API with a read-only key on `alerts/state/`, as the logs route is
**[T: not decided to be built here; #793 owns the route]**. If acknowledgement
is ever built, it is written as **separate objects** under `alerts/ack/`, which
the evaluator reads and never edits, so the evaluator stays the only writer of
state.

### Severity escalation

An incident whose severity rises for the same fingerprint keeps its key and id:
a `quality` warning that becomes a blocking error, or a `stale_output` past
`warn_after` that passes `critical_after`. The escalation sends **one**
notification (`event: escalated`) and no repeat reminders. A move from
`critical` to `warning` sends nothing; the history records it. Default, revisit.

### A policy that changes while an incident is open

| Change | Effect |
| --- | --- |
| The condition stops being declared for the scope and stage (rule removed, `false`, stage dropped, `alerts.yml` removed) | The incident resolves **silently**: `resolved_silently` in history, no notification. Its signals are ignored from then on, through the mechanism that already closes the incidents of a removed product (`active_scopes` in `alerts.evaluate`) **[V]** |
| Severity rises (`severity` option, or a freshness threshold crossed) | Re-evaluated on the next run: one `escalated` notification |
| Severity falls | No notification; history records it |
| Routes change | Routing is computed when a notification is sent (`run_once` recomputes the routed set every attempt **[V]**). An open incident's later `resolved` goes to the routes of the rule that wins then. A queued notification whose new routes are all delivered or empty leaves the outbox |
| A threshold or grace changes | Applies from the next evaluation; an incident whose condition no longer holds resolves with a notification |

### State in the ops bucket

All alert objects are under the bucket-root prefix `alerts/` of the ops bucket,
beside `audit/platform-api/` **[V]**, with one key minted for it:
`Read`, `Write` and `List` on `alerts/*`, and `Read` and `List` on each stage's
`activations/<stage>/run-artifacts/quality/*` for the manifests. It follows the
pattern of `log_reader_credentials` and `audit_writer_credentials` in
`credentials.tf` **[V]**. `Write` can delete and overwrite
(`credentials.tf`, ADR 0021), so the key is not append-only; the history is
tamper-evident against everyone but the evaluator, not immutable. Decision 10
adds the reads the alert policy needs.

| Object | Content |
| --- | --- |
| `alerts/state/v1/state.json` | `schema_version`, `evaluated_at` (written by the evaluator), the open incidents only (fingerprint, id, severity, notified severity, opened_at, last_seen, last run), the per-fingerprint id sequence, the per-stage count of consecutive unreadable evaluations, and an **outbox** of notifications not yet delivered |
| `alerts/state/v1/policy/<stage>.json` | the stage's last good alert policy: the activation revision it came from, the validated policy and the job map (Decision 10) |
| `alerts/history/v1/date=<YYYY-MM-DD>/<incident-id>-<seq>-<event>.json` | one object per event (`opened`, `escalated`, `resolved`, `resolved_silently`) and per delivery attempt (receiver name, attempt number, HTTP status or error class, time). No payload body and no URL |
| `alerts/state/v1/corrupt/<UTC time>.json` | a state object that failed to parse, kept for the operator |

Event status and delivery status are separate: an incident can be `firing` with
its notification `pending`, `delivered` or `undeliverable`.

A run of the evaluator writes in this order, which is what makes delivery
asynchronous from evaluation:

1. Read each stage's policy (Decision 10), the stage signals and the state.
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
points state and history at a different prefix. `--policy-file <path>` replaces
every stage's alert policy, so the e2e can test silence and precedence without
building a revision per case **(proposed, accepted by default 2026-11-17;
maintainer may revisit)**. All three are for the e2e only (Default, revisit:
acceptable). They let #1173 inject a missed run by moving the clock instead of
waiting for a schedule, and keep the test's state out of the real `alerts/`. The
CronJob never passes them.

## Decision 4: receivers, routing and the webhook contract

### Receivers stay in the profile

| Basis | Decision |
| --- | --- |
| Maintainer | `openlakeforge.yaml` declares the named receivers, each backed by a Kubernetes Secret the ops team owns. The alert policy refers to them by name only, through its `routes` (Decision 8). Credentials never enter the repository. |
| Maintainer | A route naming a receiver the profile does not declare fails activation, which has the profile. `olf check` validates only the policy's structure. |
| Proposed, accepted by default 2026-11-17 (maintainer may revisit) | The check runs in `deploy_revision` (`tools/olf/olf/infra/deployment/activation.py`), beside `_require_source_bindings`, before the rollout, and only when the profile declares at least one receiver. With none, no evaluator exists (Decision 1) and nothing reads the policy, so a project template that ships an `alerts.yml` still activates on a profile without receivers. |

The profile can change after a stage is activated. A route whose receiver has
since disappeared is skipped; the evaluator delivers to the routes that remain
and raises `policy_unreadable` for that stage (Decision 10).

### Routing

| Basis | Decision |
| --- | --- |
| Maintainer | A product condition goes to the routes in the `notify` of the rule that wins for it (Decision 9), mapped to receivers through the policy's `routes`. |
| Maintainer | A platform condition goes to the platform receivers (Decision 11). |
| Proposed, accepted by default 2026-11-17 (maintainer may revisit) | A receiver's optional `stages` list (for example `[prod]`) is a ceiling the ops team owns: a notification reaches a receiver only when it is routed there **and** the receiver's `stages` include the stage. Omitted means every enabled stage, so the ceiling is open by default. A stage the profile does not enable is a validation error (built in #1168). |

The ceiling means a product rule cannot page a PROD-only on-call from DEV.

### The webhook contract

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
| evaluator to SeaweedFS S3, port 8333 | F33 | with its own key (Decision 3), which also reads the alert policy (Decision 10) |
| evaluator to the receivers, port 443, any address outside the pod and Service ranges | F28, F30 | NetworkPolicy has no FQDN selector, so the rule is as wide as those two; the receiver's URL is the only thing that narrows it |

The e2e's receiver is a stub pod in `olf-system` carrying a label the flow file
names, as the Dex e2e's GitHub stub does (F29 **[V]**) **[T]**. Its listener is
HTTPS, because the adapter refuses plain HTTP.

The evaluator's ServiceAccount has no Kubernetes API access: it needs none while
service signals are not alerted.

## Decision 8: the alert policy, `lakehouse_code/alerts.yml`

| Basis | Decision |
| --- | --- |
| Maintainer | A project descriptor, `lakehouse_code/alerts.yml`, with `apiVersion: openlakeforge.io/v1beta1` and `kind: AlertPolicy`. |
| Maintainer | It follows ADR 0005: a closed JSON Schema at `docs/schema/alert-policy.schema.json` (written in slice AP-a; this record fixes its shape), a typed model in `openlakeforge_core`, both run by `olf check contracts`. |
| Maintainer | It is YAML, not Python: the evaluator reads it without importing project code. |
| Maintainer | Stage differences live inside the one file, as `stages` on each rule and in `defaults`, so one revision serves every stage (ADR 0012). |
| Maintainer | It ships in the revision's `descriptors` component (Decision 10). |

The file is optional; without it a project gets no product alerts (Decision 2).

### Shape

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `routes` | map of route name to receiver name | yes, non-empty | Names the rules use for receivers. The receiver name is a `spec.observability.alerts.receivers[].name` of the profile (Decision 4). |
| `defaults.stages` | list of `dev`, `uat`, `prod` | no; every stage | Stages a rule applies to when it names none. |
| `defaults.notify` | list of route names | no | Routes a rule notifies when it names none. |
| `rules` | list, non-empty | yes | The rules below. |
| `rules[].match.domain` | a domain `name` of `governance.yml`, or `"*"` | yes | |
| `rules[].match.product` | a product `id` of `governance.yml`, or `"*"` | yes | |
| `rules[].stages` | list of stages | no; `defaults.stages` | |
| `rules[].notify` | list of route names | no; `defaults.notify` | A rule that resolves to no route is a validation error. |
| `rules[].when` | map of condition to options, or to `false` | yes, non-empty | `false` turns off a condition that a less specific rule turns on. |

Names follow the identifier pattern the profile's receiver names use. Unknown
fields at any level are rejected, as in every descriptor (ADR 0005), and the
platform conditions (`stage_unreadable`, `policy_unreadable`) are unknown
`when` keys (Decision 11). Matching uses `governance.yml` ids: a product is
matched through its job `<id>_pipeline`, and its domain is the one it is nested
under. A job that is no product's (`gold_maintenance`, `silver_maintenance`) is
matched only by a rule whose `match` is `"*"` for both **(proposed, accepted by
default 2026-11-17; maintainer may revisit)**; the alert e2e runs on
`gold_maintenance`, so this keeps it working.

### Conditions and their options

| `when` key | Alert kind | Options | Default |
| --- | --- | --- | --- |
| `run_failed` | `run_failed` | `severity`: `warning` or `critical` | `critical` |
| `missed_run` | `missed_run` | `grace`: a duration; `severity` | `15m` (`SCHEDULE_GRACE`); `critical` |
| `freshness` | `stale_output` | `warn_after`, `critical_after`: durations since the last successful run. With neither, cadence-derived staleness applies, with `grace` (default `15m`) and `severity` (default `critical`) | cadence-derived |
| `quality` | `quality` | `level`: `warn` or `error` | `warn` |
| `schedule_mismatch` | `schedule_mismatch` | `severity` | `warning` |

`{}` turns a condition on with its defaults. A duration is a positive integer
with `m`, `h` or `d` (`30m`, `26h`, `2d`). `warn_after` must be below
`critical_after` when both are set; either may be set alone. Thresholds and
cadence options do not mix in one rule.

- **`freshness`** keeps the alert kind `stale_output` **(proposed, accepted by
  default 2026-11-17; maintainer may revisit)**, so fingerprints, payloads
  (`condition: stale_output`) and the guide's runbook anchors do not change.
- **`quality`** with `level: warn` alerts on a WARN or a non-blocking ERROR at
  `warning` and a blocking ERROR at `critical`. With `level: error`, a WARN alone
  does not alert. The signal says which outcome it saw, not only its state
  The signal carries the outcome it saw (`Signal.outcome`), so the
  evaluator never parses the reason. Floe and
  dbt keep their own thresholds; `level` only chooses which outcome alerts.
- **`missed_run`**'s `grace` replaces `SCHEDULE_GRACE` for that product, which
  also moves the evidence bound it feeds (`_evidence_bound`). Detection still
  waits for the next 5-minute evaluation.

### Example

```yaml
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy

routes:
  on-call: on-call            # route name: receiver name in openlakeforge.yaml
  sales: sales-webhook
  data-eng: data-eng-chat

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

  # DEV failures go to the engineers' channel, as warnings.
  - match: {domain: "*", product: "*"}
    stages: [dev]
    notify: [data-eng]
    when:
      run_failed: {severity: warning}

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

What it resolves to for `order_revenue` in PROD (Decision 9):

| Condition | Winning rule | Options | Notifies |
| --- | --- | --- | --- |
| `run_failed` | global | `critical` | `on-call` |
| `missed_run` | `order_revenue` | grace `30m`, `critical` | `sales`, `on-call` |
| `freshness` | `order_revenue` | warn `26h`, critical `30h` | `sales`, `on-call` |
| `quality` | `sales` domain | `level: warn` | `sales` |
| `schedule_mismatch` | global | `warning` | `on-call` |

In DEV the only match is the second rule: a failed run notifies `data-eng` as a
warning, and nothing else alerts.

### What `olf check contracts` checks offline

The schema and the typed model; that every `notify` names a key of `routes`;
that every `match` names a domain and product of `governance.yml`, and that a
named product is nested under the named domain; and that no two rules of the
same specificity (Decision 9) set the same condition for an overlapping stage.
Stage names are checked against the vocabulary (`dev`, `uat`, `prod`), not
against a profile: the same revision activates under profiles that enable
different stages, and a rule for a stage that is not enabled is inert. Receiver
names cannot be checked here (Decision 4).

An `olf alert explain --stage <stage> --product <id>` command prints the
winning rule, options and routes per condition, offline, from `alerts.yml` and
`governance.yml` **(proposed)**. It needs no credential, so it is top-level
under ADR 0020.

### Why not Dagster's own alerting

| Basis | Decision |
| --- | --- |
| Proposed | The policy is evaluated by this evaluator, not expressed as Dagster freshness policies, asset checks or sensors. |

1. **Open-source Dagster describes state; it does not tell people.** The pinned
   1.13.7's `FreshnessPolicy` gives an asset a `PASS`, `WARN`, `FAIL` or
   `UNKNOWN` freshness state **[V: its docstring in
   `dagster/_core/definitions/freshness.py` of the 1.13.7 package]**. Asset
   checks likewise record an outcome in Dagster **[T: not checked here]**.
   Routing those states to people, with alert policies, is a Dagster+ feature
   **[T: not verified against Dagster's documentation; no alert-policy type
   exists in the open-source 1.13.7 package, which is all this record
   checked]**.
2. **Telling people from open-source Dagster takes a sensor.**
   `run_failure_sensor` and `make_email_on_run_failure_sensor` are exported by
   the 1.13.7 package **[V]**, but `olf check project-code` refuses every
   sensor a code location defines (`_verify_stage_sensors` in
   `tools/olf/olf/common/project_code_check.py` **[V]**), and Decision 1
   rejects a sensor because it shares fate with the scheduler it watches.
3. **The evaluator must not import project code.** Dagster policies are Python
   objects inside the code location; reading them back needs the stage's
   Dagster to answer, which is the outage `stage_unreadable` exists for.

## Decision 9: rule precedence

| Basis | Decision |
| --- | --- |
| Maintainer | For each scope, stage and condition, the **most specific** matching rule wins: a named product, then a named domain with `product: "*"`, then the global `"*"`/`"*"`. A rule names a product whether or not it also names the domain. |
| Maintainer | `notify` is the winning rule's only, not the union of every matching rule. |
| Maintainer | Two rules at the same specificity that set the same condition for an overlapping stage are a validation error, never resolved by file order. |

The choice is per condition, so a product rule that sets only `freshness`
still takes `run_failed` from the domain or global rule. `false` in `when` wins
like any value, which is how a product opts out of a condition a domain rule
turns on.

Why the winner's `notify` and not the union: with a union, a broad rule can
never be narrowed. The example's sales quality warnings would always reach
`on-call` as well, because the global rule matches them. With the winner's
list, a team widens by naming both routes (`[sales, on-call]`) and narrows by
naming one, and every notification is explained by exactly one rule. The cost
is repetition: a product rule that wants the on-call too must say so.

## Decision 10: how the evaluator gets the policy

| Basis | Decision |
| --- | --- |
| Maintainer | `alerts.yml` is an entry of the revision's `descriptors` component. Each evaluation reads `activations/<stage>/ACTIVE.json`, then that revision's `alerts.yml` from the ops bucket. |
| Maintainer | A stage whose policy cannot be used keeps its **last good policy**, cached at `alerts/state/v1/policy/<stage>.json`, and raises a stage-scoped `policy_unreadable` warning to the platform receivers (Decision 11). |
| Maintainer | A new read grant on the `openlakeforge-alerts` key covers `project/revisions/*` (see "The read grant"). Slice AP-b tests whether SeaweedFS honours a wildcard in the middle of an action path and, if it does, narrows the grant to the `descriptors` component. |
| Proposed | The evaluator also reads the same revision's `governance.yml`, to map jobs to product ids and domains, and the revision's sidecar, to check both files' sha256 against the manifest. |
| Proposed, accepted by default 2026-11-17 (maintainer may revisit) | A stage with no activation, or whose revision has no `alerts.yml`, has an empty policy, and no warning: both are normal states. |

The read path, per stage and evaluation:

1. `activations/<stage>/ACTIVE.json`. When its activation revision equals the
   one cached for the stage, the cached policy is used and the path stops here.
2. `activations/<stage>/revisions/sha256/<digest>/ACTIVATION.json`, for the
   project revision.
3. `project/revisions/sha256/<digest>/PROJECT-REVISION.json`, then
   `descriptors/lakehouse_code/alerts.yml` and
   `descriptors/lakehouse_code/governance.yml` under the same prefix.

The pointer, activation and sidecar readers move from `tools/olf` into
`openlakeforge_core` in slice AP-b, because the evaluator image does not carry
`olf` **[V]**; `olf` then imports them.

The cache holds the activation revision, the validated policy and the job map,
inside the existing `alerts/*` grant. `policy_unreadable` fires when a read
fails, a digest does not match, the file fails validation (a revision built by
a newer distribution) or a route names a receiver the profile no longer has. It
is a `warning`, and resolves on the first evaluation that reads the stage's
policy cleanly. With no last good policy at all (the first evaluation after a
deploy), the stage evaluates with an empty policy and still raises the warning;
a product incident already open for it is held, not resolved, because a policy
that could not be read is not a rule that disappeared.
A policy error in one stage therefore degrades that stage to its last good
policy and one warning, never to silence across stages.

### The read grant

`Read` on `activations/<stage>/ACTIVE.json` and
`activations/<stage>/revisions/*` for each enabled stage, and `Read` on
`project/revisions/*`. No `List` and no `Write`. It is a row change in
`credentials.tf` and a text change to flow F39 in ADR 0017; no new flow, since
the evaluator already reaches SeaweedFS on 8333.

`project/revisions/*` holds every frozen file of every revision (dbt SQL,
Dagster code, Floe contracts), so the key reads more than the two files it
needs. The content carries no credential by construction, and the value scan
rejects concrete secrets before publication (ADR 0012); the maintainer
accepted the wider reach **[T: whether SeaweedFS honours a wildcard in the
middle of an action path, which would narrow it to
`project/revisions/sha256/*/descriptors/*`]**.

## Decision 11: platform conditions stay with the profile

| Basis | Decision |
| --- | --- |
| Maintainer | `alerts.yml` cannot name a platform condition (`stage_unreadable`, `policy_unreadable`, and the log archive's delivery signals if they ever become conditions); the schema rejects them as unknown `when` keys. |
| Maintainer | They go to the profile's platform receivers: a new optional `spec.observability.alerts.platform_receivers`, a list of receiver names. Omitted means every receiver, which is how they were routed before the policy. Each receiver's `stages` ceiling still applies. |

The product team owns `alerts.yml`; the ops team owns the cluster, the
receivers and the Secrets. A Dagster that cannot be read, or a policy the
evaluator cannot use, is the ops team's problem whatever the product, and
routing it from a file the ops team does not own would let one bad revision
silence the platform's own alarms. The field is a schema change to
`DeploymentProfile`, so slice AP-c records it in ADR 0011 and the deployment
profile reference.

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
  earlier than the due time plus its grace.
- A product team changes what alerts, when and to whom by a pull request to
  `alerts.yml`, reviewed with the code it watches and promoted with the same
  revision. The ops team keeps the receivers, their credentials and a
  per-receiver stage ceiling; a revision can never add a webhook.
- Upgrading to the policy is a behaviour change: a project must ship
  `alerts.yml` to keep product alerts.
- The evaluator key reads project revisions, a wider reach than the state and
  manifests alone needed.
- The portal and the health routes keep the cadence-derived view **(proposed,
  accepted by default 2026-11-17; maintainer may revisit)**; a team's freshness
  thresholds show up only as alerts.

## Retention

`alerts/history/` is kept for **30 days**; `alerts/state/` is never expired. Like
ADR 0022, decision 6, retention is a lifecycle rule on the prefix through the ops
bucket contract, and whether SeaweedFS 4.48 honours bucket lifecycle rules is
**[T]**: no lifecycle rule reaches any prefix today **[V: the
`logs_archive_retention` comment in `environments/local/contracts.tf`]**. Until
a test shows it does, the 30 days is a request, not a guarantee, and the
evaluator does not delete history itself.

## Open items

Things this record had to decide for the evaluator because #23 did not. Each is
marked Proposed above; the maintainer may change any of them.

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

## Questions answered on 2026-11-17

The alert policy's design put eleven questions to the maintainer (PR #1203).
None stays open.

| # | Question | Answer | Basis | Where |
| --- | --- | --- | --- | --- |
| 1 | `notify`: the winner's or the union? | The most specific rule's only; same-level overlaps are a validation error | Maintainer | Decision 9 |
| 2 | Who routes platform conditions? | `spec.observability.alerts.platform_receivers` (omitted: every receiver), never `alerts.yml` | Maintainer | Decision 11 |
| 3 | A policy that cannot be used | Keep the last good policy at `alerts/state/v1/policy/<stage>.json`; raise `policy_unreadable` to the platform receivers | Maintainer | Decision 10 |
| 4 | The width of the read grant | Accept `project/revisions/*`; slice AP-b tests a mid-path wildcard and narrows the grant if SeaweedFS honours it | Maintainer | Decision 10 |
| 5 | The receiver `stages` filter | Kept, as an ops-owned ceiling | Proposed, accepted by default 2026-11-17 (maintainer may revisit) | Decision 4 |
| 6 | Jobs of no product | Matched only by the global `"*"`/`"*"` rule | Proposed, accepted by default 2026-11-17 (maintainer may revisit) | Decision 8 |
| 7 | `freshness` and `stale_output` | The alert kind stays `stale_output` | Proposed, accepted by default 2026-11-17 (maintainer may revisit) | Decision 8 |
| 8 | Thresholds without a running schedule | They apply in any schedule state | Proposed, accepted by default 2026-11-17 (maintainer may revisit) | Decision 2 |
| 9 | The health routes and the policy | The routes stay policy-free in v0.7 | Proposed, accepted by default 2026-11-17 (maintainer may revisit) | Build order |
| 10 | The route check without receivers | Skipped when the profile declares none | Proposed, accepted by default 2026-11-17 (maintainer may revisit) | Decision 4 |
| 11 | An e2e flag for the policy | `--policy-file`, for the e2e only | Proposed, accepted by default 2026-11-17 (maintainer may revisit) | Decision 3 |

## Build order

### The evaluator (built)

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

### The alert policy (AP-a and AP-b built, AP-c in progress)

Each slice stays under the 15-file, 600-line cap in AGENTS.md. AP-c is where
behaviour changes, so the sample policy (AP-a), the read path (AP-b) and the
migration land before or with it.

| Slice | Content | Updates |
| --- | --- | --- |
| AP-a | `docs/schema/alert-policy.schema.json`; `openlakeforge_core/alert_policy.py` with the precedence resolver; `olf check contracts`; `olf alert explain`; the sample project's `alerts.yml`. No runtime reads it yet | ADR 0005; `docs/reference/descriptors.md` |
| AP-b | `alerts.yml` in the `descriptors` component; the pointer, activation and sidecar readers in core; the evaluator's read path, last good policy and `policy_unreadable`; the read grant and its wildcard test; the activation route check. The evaluator reads the policy and does not apply it yet | ADR 0012; ADR 0017 (F39) |
| AP-c | `alerts.evaluate` takes the resolved policy; options, thresholds and routing; `pipeline_health` thresholds; `platform_receivers`; `--policy-file`; the conformance profile's receiver rename; the e2e cases for silence, precedence and thresholds | ADR 0011; `docs/reference/deployment-profile.md` |
| AP-d | The alerts guide (the migration callout, the policy, each condition's options), the policy reference, the health-signals page | `docs/guides/operations/alerts.md`, `docs/reference/` |

What the policy slices change in the merged code:

| File | Change |
| --- | --- |
| `packages/core/openlakeforge_core/alert_policy.py` (new) | `AlertPolicy`, `AlertRule`, condition options; `load_alert_policy`, `validate_alert_policy`; `resolve_policy(policy, governance, stage) -> ResolvedPolicy`, which answers per job and condition: enabled, options, route names |
| `packages/core/openlakeforge_core/alerts.py` | `evaluate(previous, signals, now, *, policy: ResolvedPolicy, ...)`. A signal whose condition the policy does not enable for its scope is ignored, and its open incident resolves silently. `_severity_of` takes the severity from the options instead of constants; `quality` applies `level`; `stale_output` can open at `warning` |
| `packages/core/openlakeforge_core/health.py` | `evaluate_freshness` gains an age mode: `warn_after` and `critical_after`, measured from the last success's end, for any schedule state |
| `packages/platform-api/openlakeforge_platform_api/pipeline_health.py` | `read_stage_pipelines` and `_evaluate` take per-job grace and thresholds from the resolved policy. `SCHEDULE_GRACE` becomes the default grace and `RUN_MAX_AGE` stays the bound for a job with no running schedule. The health routes call it without a policy, so they keep the cadence-derived signals |
| `packages/platform-api/openlakeforge_platform_api/alerting/run.py` | Reads each stage's policy (Decision 10) before evaluating. Routing replaces `receiver.routes(stage)` with the winning rule's routes, mapped to receivers and capped by each receiver's `stages`; platform conditions route to the platform receivers |
| `packages/platform-api/openlakeforge_platform_api/alerting/settings.py` | `ReceiverSetting.stages` is kept as the ceiling. New: the ops bucket's activation and revision prefixes per stage, and `platform_receivers` |
| `packages/platform-api/openlakeforge_platform_api/alerting/__main__.py` | `--policy-file` (Decision 3) |
| `tools/olf/olf/infra/project_revision.py` | `_descriptors_component` adds `alerts.yml` when it exists; the component is a path map, so the revision schema does not change **[V: `ComponentEntries.entries`]** |
| `tools/olf/olf/infra/deployment/activation.py` | The route check of Decision 4 |
| `tools/olf/olf/common/profile.py` | `platform_receivers` (Decision 11) |
| `infra/terraform/modules/storage/seaweedfs/credentials.tf` | The read grant of Decision 10 |

## Rejected

| Option | Why not |
| --- | --- |
| A Dagster sensor | Decision 1: it shares fate with the scheduler it watches and lives in user project code |
| Dagster freshness policies or asset checks as the policy | Decision 8: they describe state, and routing it to people needs Dagster+ or a sensor |
| Prometheus Alertmanager | Needs #21, which Slim does not run; the alerts of #23 must work without it |
| Acknowledgement in v0.7 | Maintainer: no route, no reminders, so nothing needs an ack |
| Reminders while an incident stays open | Maintainer: one notification on fire, one on resolve |
| HMAC-signed bodies | Maintainer: the URL is the credential in v0.7 |
| Following redirects | Maintainer: a redirect can leave HTTPS or the receiver the operator chose |
| State in PostgreSQL | The API is stateless until a feature needs storage (ADR 0021); a bucket object is enough for one writer and costs no schema |
| Several adapters at once | #23: one webhook first, a provider-neutral interface for the rest |
| Alert rules in Python inside the code location | The evaluator would have to import project code (Decision 8, maintainer) |
| Receivers declared in `alerts.yml` | Credentials and Secrets are the ops team's; the revision would carry deployment intent (ADR 0012) |
| One policy file per stage | One revision serves every stage (ADR 0012); stage differences live in `stages` (maintainer) |
| The policy in `openlakeforge.yaml` | Product teams would edit the ops team's profile, and the policy would not be reviewed with the product code |
| Alert by default, opt out per product | Maintainer: silent unless declared |
| `notify` as the union of every matching rule | Decision 9: a broad rule could never be narrowed |

## History

2026-11-16: created for #1167 (slice 23-a of #23).

2026-11-16: the CronJob, the `openlakeforge-alerts` key and flows F38 to F40 built for #1172 (slice 23-e); local
reports `alerts.supported = true`. The Secret check is `require_alert_receiver_secrets` in
`tools/olf/olf/infra/deployment/local/platform.py`, an `olf` precondition before the apply. The evaluator's variables are named by receiver position
(`OPENLAKEFORGE_ALERT_RECEIVER_<n>`), and its state URI is `s3://<ops bucket>/alerts/`. No deviation from the decisions above.

2026-11-16: the evaluator saves its state after every send attempt and stops starting sends after a 150 s budget, under the
CronJob's 240 s deadline, so slow receivers cannot kill the Job before progress is recorded; a starved entry still ages
through `runs` and expires. With `local-ca`, the evaluator trusts the public roots plus the cluster CA (a bundle built at
startup in `/tmp`) rather than the cluster CA alone. No deviation from the decisions above.

2026-11-16: quality signals are read from the per-run manifests (#1186, slice
c of #793); the health routes report the quality integration as `available` or
`not_provided`.

2026-11-16: quality `healthy` needs a successful run whose manifest is
`complete`, a flag the run's last publish sets once dbt's evidence is read; a
pass in an incomplete manifest reads `unknown`. Runs are read newest first: one
that did not succeed answers only with a `warning` or `critical`, so a retry's
partial manifest cannot resolve the previous run's quality incident, even when
an older success precedes the failure. A success after a newer run whose
manifest could not be read reads `unknown`, not `healthy`, so a transient read
failure neither resolves nor reopens the
incident.

2026-11-16: the deployed evaluator reads the quality manifests: its settings
carry `quality_manifests` and it builds one reader per stage on its own
`openlakeforge-alerts` key, not the platform API's per-stage keys, so no Secret,
grant or network row was added. No deviation from the decisions above.

2026-11-16: the e2e (`olf/infra/e2e/_alerts.py`, #1173, slice 23-f) runs the evaluator as Jobs made from the CronJob with
`--state-prefix alerts/e2e/` against a stub receiver, flow F41; the prefix sits inside the evaluator key's `alerts/*` grant, because `--state-prefix` replaces the configured prefix. The Job reads its own settings, with `ca_file` set to the CA that signed the stub's certificate, so the delivery does not depend on the route TLS mode, and the e2e runs only when the stub is the evaluator's sole receiver. Its delivery records are the `-delivery-` objects under
that prefix's `history/v1/`, not a separate `alerts/deliveries/` prefix. The e2e writes the Floe WARN and ERROR cases as
manifests for a real run, as the evaluator reads only the manifest. No deviation from the decisions above.

2026-11-17: alert policy as code (PR #1203). Which conditions alert, their
thresholds and who is told move from fixed code to a project's
`lakehouse_code/alerts.yml` (`AlertPolicy`), with maintainer decisions:
silent unless declared, freshness thresholds, receivers kept in the profile and
named through `routes`, the most specific rule's `notify`, platform conditions
routed by `platform_receivers`, the policy read from the stage's active
revision with a last good cache and `policy_unreadable`, and the
`project/revisions/*` read grant. Decision 2 became the declared conditions
with their defaults and options; Decision 4's stage routing became rule routing
with the receiver's `stages` as a ceiling; Decisions 8 to 11, the answered
questions and the AP slices were added. The Status now says the evaluator
slices are built; it still read "not built". The design was first drafted as a
separate ADR 0024 and folded in here, per AGENTS.md rule 8.

2026-11-17: quality key renamed from `on` to `level`, because YAML 1.1 parsers read `on` as true (maintainer).

2026-11-17: alert policy slice AP-b built (#1208, #1209 and the grant PR stacked on them). The evaluator now reads each
stage's policy and does not apply it: `run_once` reads and logs it, and the alerts that fire are unchanged until AP-c.
`alerts.yml` joins the `descriptors` component when the project has one **[V: `_descriptors_component`,
`olf/infra/project_revision.py`]**, and the pointer, activation and sidecar readers moved to `openlakeforge_core`
(`project_activation.py`, `project_revision.py`, `stages.py`) with `olf` importing them **[V]**. The reader is
`alerting/policy.py`. Two details this record left open: the cache at `alerts/state/v1/policy/<stage>.json` holds the
activation revision, the `alerts.yml` document and the product ids of `governance.yml`, and the job map is derived
from them on each read, so a newer `openlakeforge_core` re-validates the cache and discards one it can no longer
parse; and a route naming a receiver the profile no longer declares is reported as `policy_unreadable` while the
policy itself is still used and cached. The read grant is `Read` on each enabled stage's `ACTIVE.json` and
`revisions/*` and on `project/revisions/*` (`credentials.tf`, tested in `alerts_identity.tftest.hcl`); no flow, Secret
or e2e network pin changed, since the evaluator already reaches the gateway over F39 **[V: the grant is a key action
list, not a flow]**. The activation route check runs in `deploy_revision` beside `_require_source_bindings` and is
skipped when the profile declares no receiver **[V]**; it also refuses an `alerts.yml` that cannot be parsed while
receivers exist, because its routes cannot then be checked. Still open **[T]**: whether SeaweedFS honours a `*` in the
middle of an action path. It could not be run without a cluster, so the accepted `project/revisions/*` grant stands and
the narrowing to `project/revisions/sha256/*/descriptors/*` is not done.

2026-11-17: alert policy slice AP-c, part one: the evaluator applies the policy. `alerts.evaluate` takes each stage's
`ResolvedPolicy`: a product condition opens only where a rule enables it for the job, takes its severity from the
rule's options, and an incident whose condition stops being enabled resolves silently; fingerprints are unchanged and a
severity the rule changes is re-evaluated on the next run (one `escalated` notification when it rises, none when it
falls). `read_stage_pipelines` takes the policy and applies `missed_run`'s `grace` (also moving the evidence bound) and
`freshness`'s `grace` or `warn_after`/`critical_after`, the latter through the new `evaluate_freshness_age`, in any
schedule state; the health routes pass no policy. `Signal` carries the quality `outcome`, which answers the earlier
**[T]**: `level: error` is applied while the manifest is read (`quality_signal(level=)`): a WARN counts as a pass, so
every safeguard of the read applies to it unchanged. A running retry's incomplete manifest proves nothing, a newer run's
WARN does not hide an older run's blocking ERROR, a manifest holding a WARN and a non-blocking ERROR reports the ERROR
whatever the order of its checks, and a complete retry holding only WARNs (or PASSED and WARN) resolves the incident.
A raised `missed_run` grace resolves the incident
opened for the same occurrence (the signal carries when its occurrence came due), while a later occurrence inside its
grace still clears nothing. Two details this record left open: a
stage whose policy has never read cleanly and cannot be read now is left out of the evaluation's policy, so its open
product incidents are held rather than resolved silently, and nothing new opens for it; and a severity that falls
is not written to history, which has no event for it (the incident's stored severity is updated).
