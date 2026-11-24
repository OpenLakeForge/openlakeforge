# ADR 0023: Alert evaluation, alert policy, incident state and webhook delivery

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ (evaluator #1168 to #1173, alert policy #1203 and slices AP-a to AP-d; `alerts.supported = true`) · AWS ✘ · Azure ✘ (`alerts.supported = false`) |

## Context

[#23](https://github.com/OpenLakeForge/openlakeforge/issues/23) asks for a
notification when a run fails, a scheduled run never starts, output goes stale
or a quality check warns, without Prometheus, Loki, Grafana or a SaaS, and on
Slim. A missed run produces no event, so something outside Dagster has to look
for it. The health vocabulary and its rules already exist in
`openlakeforge_core.health` and the platform API's health routes
([health signals](../reference/health-signals.md)). One revision serves every
stage ([ADR 0012](0012-project-revisions-and-promotion.md)), the ops team owns
the profile, the cluster and its Secrets, and a product team owns its project
code. So the evaluator is a platform component, what alerts is declared by the
product team in the revision, and who can be told, with which credential, is
declared by the ops team in the profile.

## Decision

**D1. One evaluator, a CronJob in `olf-system`, runs every 5 minutes on the
platform API image; it exists only when the profile declares a receiver.**
It runs `python -m openlakeforge_platform_api.alerting` with schedule
`*/5 * * * *` and `concurrencyPolicy: Forbid`, imports the same health rules
and Dagster client as the API, and needs no second image. One evaluator reads
every enabled stage and writes one state, so incidents are deduplicated and
routed once. It is not a Dagster sensor: a sensor shares fate with the
scheduler it watches, lives in user code deployed by the artifacts phase
(AGENTS.md rule 3) and sees one stage. Nothing in alerting depends on
Prometheus. With no receiver no CronJob, key or flow is rendered; the Portal
and CLI warnings come from the live health routes and do not depend on it.
Enforced by: `infra/terraform/modules/observability/alerts/main.tf`.

**D2. A product condition alerts only where the project's alert policy (D8)
declares it for that product and stage; everything else is silent.** A project
without `alerts.yml` gets no product alerts. The conditions, each one incident
per scope that notifies on open and on resolve (D3):

- `run_failed`: the latest run of the job failed; default `critical`. It
  resolves only when a later run succeeds, never while a retry is in progress.
- `missed_run`: a running schedule's due time is past its grace (default 15
  minutes) and no run started since (`expected_run` is `critical`); default
  `critical`. It resolves when a run starts at or after the due time, and
  resolves silently when the schedule is stopped while it is open.
- `stale_output` (declared as `freshness`): with `warn_after` or
  `critical_after`, the last success is older than the threshold, in any
  schedule state; without them, a running schedule's job has no success since
  the previous due time plus grace, default `critical`. It resolves on a
  success at or after the due time (or younger than `warn_after`), never on a
  schedule merely switched on. It is suppressed while `missed_run` or
  `run_failed` is open for the same job, which already explains it.
- `quality`: the latest quality outcome, read from the run's quality manifest
  through the same reader as the health routes, is `warn` or a non-blocking
  `error` (`warning`) or a blocking `error` (`critical`), one incident with two
  severities. It resolves when a later run's outcome is `passed`. A run with no
  manifest reads `unknown` and opens nothing.
- `schedule_mismatch`: a PROD schedule the product's cadence declares running
  is stopped in Dagster; default `warning`. It resolves silently when the
  declaration or the schedule changes.

The platform conditions (`stage_unreadable`, `policy_unreadable`,
`service_degraded`, `health_unreadable`) are D10's and D11's. `stage_unreadable`
opens at `critical` after 3 consecutive evaluations that cannot read a stage's
Dagster, holds the stage's open incidents meanwhile, and resolves on the first
evaluation that reads it. Never an alert: a canceled run, a single `unknown`
signal (it neither opens nor resolves), a `disabled` or absent schedule for
`missed_run` and cadence-derived `stale_output`, resource or rejection-rate
thresholds, and product `service` signals. A job with no success among the runs
read stays `unknown`. Full Service Level Agreements stay with #921.
Enforced by: `packages/core/openlakeforge_core/alerts.py`,
`packages/core/openlakeforge_core/health.py`.

**D3. An incident notifies once when it opens, once when it resolves and once
when its severity rises; its state lives in the ops bucket under `alerts/`,
written by the evaluator alone.** An incident's fingerprint hashes the model
version, stage, condition, domain, product, table and component, never the run
id or the policy, so repeated failures and edited rules stay one incident; a
recurrence after a resolve gets a new id `<fingerprint>-<opened_at>-<seq>`.
There are no reminders and no acknowledgement: no route, state or POST for it,
and if one is ever built it is separate objects under `alerts/ack/` that the
evaluator only reads. A rise in severity sends one `escalated`, compared with
the highest severity already notified; a fall, or a rise back after a fall,
sends nothing and is recorded as `severity_changed`. An incident whose condition
stops being declared resolves silently (`resolved_silently`). Each run records
its events in an outbox and writes the state before it delivers, so delivery is
at-least-once: better a duplicate than silence. If the bucket cannot be read or
written, the evaluator delivers nothing and exits non-zero. A missing or
unparsable state is an empty state, kept aside under `corrupt/` if it was
unparsable, and every firing incident is notified again under a new id.
Conditional writes are not relied on; `Forbid` is what keeps one writer, and an
overlap after a node loss costs a duplicate. The platform API reads the state
with a read-only key (`GET /api/v1/stages/{stage}/alerts/incidents`) and never
writes it. `--now`, `--state-prefix` and `--policy-file` are for the e2e only;
the CronJob never passes them. History under `alerts/history/` is kept 30 days,
as a filer path TTL of [ADR 0022](0022-pod-log-collection-and-archive-format.md)
D6 on that prefix, independent of the log archive's retention; `alerts/state/`
never expires; the evaluator deletes nothing itself.
Layout and write order: [incidents and state](../guides/operations/alerts.md#incidents-and-state).
Enforced by: `packages/core/openlakeforge_core/alerts.py`,
`packages/core/openlakeforge_core/alerts_state.py`,
`packages/platform-api/openlakeforge_platform_api/alerting/store.py`.

**D4. Receivers are declared in the profile, and the one adapter is an HTTPS
webhook whose Secret-held URL is the credential.** `openlakeforge.yaml` names
each receiver, its `type` (`webhook` only) and a `secret_ref` to a Secret the
ops team creates in `olf-system`; credentials never enter the repository, and
only the reference may reach Terraform state. The deploy fails, naming the
receiver, when the Secret or its key is missing, through an `olf` precondition
that asks the cluster without reading the value, not a Terraform data source.
The alert policy names receivers only through its `routes`; activating a
revision fails when a route names a receiver the profile does not declare,
checked only when the profile declares at least one receiver. A receiver's
optional `stages` is a ceiling the ops team owns: a notification reaches a
receiver only when routed there and the stage is in its `stages` (omitted:
every enabled stage). The webhook: HTTPS only, checked before any request;
redirects are not followed and count as a rejection; no separate token and no
HMAC signing; `POST` of JSON with `Idempotency-Key: <incident id>:<event>`,
identical on every retry. Connection errors, 408, 429 and 5xx are retried
within a bounded run and across later runs until the notification expires as
`undeliverable`; any other 4xx is a rejection, never resent. The URL is never
logged, stored in state or history, or sent in a payload; logs name the
receiver. The body is built from a closed field list, and never carries log
lines, run tags, error text, row contents, storage URLs or credentials. A later
adapter implements the same notifier interface as a new receiver `type`.
Numbers and payload: [delivery](../guides/operations/alerts.md#delivery) and
[a notification](../guides/operations/alerts.md#a-notification).
Enforced by: `packages/lakehouse-services/openlakeforge_services/notify.py`,
`require_alert_receiver_secrets` in
`tools/olf/olf/infra/deployment/local/platform.py`, the route check in
`tools/olf/olf/infra/deployment/activation.py`.

**D5. A notification links the run in the stage's Dagster and the runbook
section of its condition.** `links.run` is
`<scheme>://dagster-<stage>.<base_domain>/runs/<run_id>`, with the scheme taken
from the access contract (`http` only for `spec.access.tls: none`), omitted
without `spec.access`. It goes through the perimeter, so the reader must sign in
and hold a role ([ADR 0013](0013-service-access-and-ingress.md),
[ADR 0014](0014-identity-and-authorization.md)); the evaluator never mints a
token for it. `links.runbook` is `<runbook page>#<condition>`: the alerts
guide's per-condition sections, headed by each `AlertKind` value, are the
runbook. The local root defaults the page to that guide on GitHub at `main`,
overridable with `runbook_base_url` (https, no fragment), and the platform API's
incident answer carries the same link. `links.portal` is omitted until #333.
Enforced by: `test_every_condition_has_a_runbook_section_its_link_anchors_at`
and `test_the_default_runbook_link_names_a_ref_at_which_the_runbook_exists` in
`tools/olf/tests/common/test_alerts.py`.

**D6. An evaluator inside the cluster cannot report that the cluster is gone,
and this record does not work around it.** A full-cluster outage, a node pool
that cannot schedule `olf-system`, a stopped CronJob controller or an
unreachable object store produce no alert; the last leaves failed Jobs.
`state.json` carries `evaluated_at` so an outside monitor can tell a stopped
evaluator from a quiet one. An external heartbeat is a later option, not built.
Enforced by: nothing; it is a stated limit.

**D7. The evaluator reaches only Dagster, the object store, its receivers and
the platform API, each through a row of the flow file.** Its flows are F38
(each stage's Dagster, read-only GraphQL), F39 (SeaweedFS S3 with its own key),
F40 (receivers on 443, any address, since NetworkPolicy has no FQDN selector),
F41 (the e2e's stub receiver) and F44 (the platform API's services route). Its
ServiceAccount has no Kubernetes API access and its default token is not
mounted; its one identity is a projected ServiceAccount token for the platform
API (D11). Enforced by: `release/network-flows.yaml`
([ADR 0017](0017-local-network-policy-and-rbac.md)).

**D8. The alert policy is `lakehouse_code/alerts.yml`, an `AlertPolicy`
descriptor frozen in the project revision.** `apiVersion:
openlakeforge.io/v1beta1`, a closed JSON Schema and a typed model in
`openlakeforge_core`, run by `olf check contracts`
([ADR 0005](0005-descriptor-model.md)). It is YAML, so the evaluator never
imports project code, and stage differences live in each rule's `stages` and in
`defaults`, so one revision serves every stage. It ships in the revision's
`descriptors` component and is optional. Rules match `governance.yml` domains
and products; a product is matched through its job `<id>_pipeline`, and a job
of no product (`gold_maintenance`, `silver_maintenance`) only by a rule whose
`match` is `"*"` for both. `freshness` opens the alert kind `stale_output`, so
fingerprints, payloads and runbook anchors are stable. `quality`'s `level` is
applied where the manifest is read, not by rewriting the signal. Platform
conditions are not `when` keys. `olf alert explain` prints the result offline.
Shape: [alert policy reference](../reference/alert-policy.md).
Enforced by: `docs/schema/alert-policy.schema.json`,
`packages/core/openlakeforge_core/alert_policy.py`.

**D9. For each scope, stage and condition the most specific matching rule wins,
and only its `notify` applies.** A rule that names a product beats one that
names a domain with `product: "*"`, which beats the global `"*"`/`"*"`. The
choice is per condition, and `false` wins like any value, which is how a product
opts out. `notify` is the winner's, not the union, so a broad rule can be
narrowed and every notification is explained by one rule. Two rules at the same
specificity that set the same condition for an overlapping stage are a
validation error, never resolved by file order.
Enforced by: `resolve_policy` in `packages/core/openlakeforge_core/alert_policy.py`.

**D10. The evaluator reads each stage's policy from that stage's active
revision and keeps the last good one.** Each evaluation follows
`activations/<stage>/ACTIVE.json` to the revision and reads its `alerts.yml` and
`governance.yml`, checking both against the revision sidecar's digests. A stage
with no activation, or a revision without `alerts.yml`, has an empty policy and
raises nothing. A policy that cannot be used (a failed read, a digest mismatch,
a file that fails validation, a route whose receiver the profile lost) keeps the
cached last good policy at `alerts/state/v1/policy/<stage>.json` and raises
`policy_unreadable` at `warning` to the platform receivers, resolving on the
first clean read. With no last good policy the stage's open product incidents
are held and queued notifications stay queued: an unreadable policy is not a
rule that disappeared. One stage's error never silences another. The read
grant is `Read` on each enabled stage's `activations/<stage>/ACTIVE.json*` and
`activations/<stage>/revisions/*`, and on `project/revisions/*`; the wider reach
is accepted, and narrows to the `descriptors` component only if SeaweedFS
honours a mid-path wildcard. Read path: [alert policy
reference](../reference/alert-policy.md#how-the-evaluator-reads-the-policy).
Enforced by: `packages/platform-api/openlakeforge_platform_api/alerting/policy.py`,
`infra/terraform/modules/storage/seaweedfs/credentials.tf`.

**D11. Platform conditions go to the profile's platform receivers, and
platform service signals are read from the platform API.**
`spec.observability.alerts.platform_receivers` lists receiver names; omitted
means every receiver, and a present list must be non-empty. Each receiver's
`stages` still applies. Platform conditions follow D3's rules. `service_degraded`
is one incident per stage and component: `dagster.workloads` and
`trino.workloads` at the signal's state, and `object-store.list`,
`log-archive.spool-age`, `log-archive.spool-size` and
`log-archive.newest-object` at `critical` only; an `unknown` or `disabled` signal
neither opens nor resolves, and any other component is ignored. It resolves when
the component reads `healthy`. The evaluator runs no probe: it reads
`GET /api/v1/stages/{stage}/health/services`, which makes no Dagster read, as a
service principal holding a projected ServiceAccount token the API checks with a
TokenReview ([ADR 0021](0021-platform-api.md), decision 3). A route that cannot
be read sends no component signal, so open `service_degraded` incidents are held,
and after 3 consecutive such evaluations `health_unreadable` opens at
`critical`, resolving on the first read. A notification carries fixed text per
component, never the probe's reason.
Enforced by: `packages/core/openlakeforge_core/alerts.py`,
`packages/platform-api/openlakeforge_platform_api/alerting/service_health.py`,
`tools/olf/olf/common/profile.py`.

## Consequences

- The platform gains one CronJob and one object-store key, and only on a profile
  with receivers. Slim gains no resident component.
- The API and the evaluator share an image, so they are released together and
  cannot drift on the health rules; the API stays stateless.
- Detection latency is at least 5 minutes, and a missed run is reported no
  earlier than its due time plus grace.
- Delivery is at-least-once: a receiver deduplicates on `Idempotency-Key` or
  tolerates a repeat. A rejected notification is not recovered, and with no
  reminders the incident's next notification is its resolve or escalation.
- The webhook URL is a bearer credential: whoever reads the Secret in
  `olf-system` can post as the platform. Rotation is replacing the Secret.
- A project must ship `alerts.yml` to have product alerts; upgrading from the
  pre-policy build resolves uncovered incidents silently.
- The evaluator key reads every frozen file of every revision, wider than the
  two files it needs; whether SeaweedFS honours a mid-path wildcard is untested.
- The portal and the health routes keep the cadence-derived view; a team's
  freshness thresholds show only as alerts.
- Only a store that answers the evaluator but fails the API's probe can be
  reported as unreachable.
- History retention reaches only records written after its rule was set;
  history from before it never expires.
- AWS and Azure keep `alerts.supported = false`; the receivers field is in their
  contract and nothing acts on it.

## Boundaries

| Not decided here | Where |
| --- | --- |
| The health states, signal kinds and how each signal is computed | [Health signals](../reference/health-signals.md), `openlakeforge_core.health` |
| The platform API's routes, roles and service principals | [ADR 0021](0021-platform-api.md) |
| Flow enforcement and the flow file | [ADR 0017](0017-local-network-policy-and-rbac.md), `release/network-flows.yaml` |
| Receiver fields of the profile | [ADR 0011](0011-deployment-profile-and-stages.md), [deployment profile](../reference/deployment-profile.md#specobservability) |
| Revisions, activation and the descriptors component | [ADR 0012](0012-project-revisions-and-promotion.md) |
| Prometheus-sourced alerts | [ADR 0024](0024-metrics-prometheus-reference-and-read-path.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| A Dagster sensor | Shares fate with the scheduler it watches, lives in user code, sees one stage |
| Dagster freshness policies or asset checks as the policy | Open-source Dagster records state but routes it to nobody without Dagster+ or a sensor, and `olf check project-code` refuses sensors |
| Prometheus Alertmanager | Needs Prometheus, which Slim does not run |
| Acknowledgement | No reminders, so nothing needs one |
| Reminders while an incident stays open | One notification on fire, one on resolve |
| HMAC-signed bodies | The URL is the credential |
| Following redirects | A redirect can leave HTTPS or the receiver the operator chose |
| State in PostgreSQL | The API is stateless until a feature needs storage; one writer needs only an object |
| Several adapters at once | One webhook first, a provider-neutral interface for the rest |
| The evaluator probing services itself | A Role per stage, more flows and an archive read, and a second judge of the same service |
| Alert rules in Python inside the code location | The evaluator would import project code |
| Receivers declared in `alerts.yml` | Credentials are the ops team's; the revision would carry deployment intent |
| One policy file per stage | One revision serves every stage |
| The policy in `openlakeforge.yaml` | Product teams would edit the ops team's profile, unreviewed with the code |
| Alert by default, opt out per product | Silent unless declared |
| `notify` as the union of every matching rule | A broad rule could never be narrowed |

## History

- 2026-11-16 (#1167): New record; D1 to D7 for the evaluator (#23).
- 2026-11-17 (#1203): D2 became the conditions the alert policy declares; D4's
  stage routing became rule routing with the receiver's `stages` as a ceiling;
  D8 to D11 added.
- 2026-11-17: D8's quality option renamed from `on` to `level`.
- 2026-11-17: D3 records a fall in severity as `severity_changed`; D10 holds a
  stage's incidents and queued notifications when no policy was ever read
  cleanly; D11 requires a present `platform_receivers` to be non-empty.
- 2026-11-17: D11 adds platform service conditions (`service_degraded`,
  `health_unreadable`), read from the platform API with a TokenReviewed token,
  then from the services route rather than the full stage health route.
- 2026-11-17: D10's pointer grant became `ACTIVE.json*`.
- 2026-11-18 (#1280): D8's `olf alert explain` takes `--job`.
- 2026-11-18 (#1258): D3's incidents readable through the platform API.
- 2026-11-20 (#794): D5 adds the per-condition runbook link.
- 2026-11-21 (#1344): D5's default runbook page is the guide at `main`.
- 2026-11-22 (#1385): Corrected to match the code; no decision changed.
- 2026-11-23: Rewritten in the standard form; decisions 1 to 11 kept as D1 to
  D11; tables moved to the alerts guide and the alert policy reference; no
  decision changed.
- 2026-11-23: D3's 30-day history retention is applied: the seaweedfs module's
  `object_retention` takes days per prefix, and the local root gives
  `alerts/history/` 30 days and `alerts/state/` no rule.
