# ADR 0024: Alert policy as code

## Status

Decided, **not built**. Nothing in `infra/`, `tools/olf` or `packages/` changes
with this record. It takes over two parts of
[ADR 0023](0023-alert-evaluation-and-webhook-delivery.md): which conditions
alert, with which thresholds, and who is told. Evaluation, incident state and
webhook delivery stay in ADR 0023. Slices 24-a to 24-d build it (see "Build
order").

Tags used below: **[V]** checked in this repository at the commit this record
was written against (`373dbc7`), or in the pinned upstream package named
beside it; **[T]** a claim the implementing slice must test, or an upstream
claim not verified here, before anyone relies on it.

Every decision carries a basis. **Maintainer** means the maintainer chose it on
2026-11-17 and it binds every slice. **Proposed** means this record had to fill
a gap; each one is listed again under "Open questions" with a recommendation.

## Context

The evaluator built for ADR 0023 runs fixed rules:

| Fact | Where it is verified |
| --- | --- |
| The conditions and their severities are code: `AlertKind` (`run_failed`, `missed_run`, `schedule_mismatch`, `stale_output`, `quality`, `stage_unreadable`) and `_severity_of` | `packages/core/openlakeforge_core/alerts.py` **[V]** |
| The missed-run grace and the evidence bound are constants: `SCHEDULE_GRACE` (15 minutes) and `RUN_MAX_AGE` (7 days) | `packages/platform-api/openlakeforge_platform_api/pipeline_health.py` **[V]** |
| Freshness is cadence-derived only: `critical` when no run succeeded since the owed due time plus the grace, `disabled` without a running schedule | `evaluate_freshness` in `packages/core/openlakeforge_core/health.py` **[V]** |
| The only routing is the receiver list with its `stages` filter: each notification goes to every receiver whose `stages` include the incident's stage | `ReceiverSetting.routes` in `alerting/settings.py`; `run_once` in `alerting/run.py` **[V]** |
| An incident's scope carries the Dagster job as `product` (`order_revenue_pipeline`), and no domain | `_evaluate` in `pipeline_health.py`; `_read_stages` in `alerting/run.py` **[V]** |
| A product's job is `<id>_pipeline`; `gold_maintenance` and `silver_maintenance` are jobs of no product | `Product.job_name` in `openlakeforge_core/inventory.py`; `product_dagster.py` **[V]** |
| Only PROD carries schedules; DEV and UAT are manual, because one revision serves every stage | `_SCHEDULED_STAGES` in `packages/runtime/openlakeforge_runtime/product_dagster.py` **[V]** |
| One `ProjectRevision` serves every stage; its `descriptors` component freezes `governance.yml` and each `source.yaml`, published under `project/revisions/sha256/<digest>/` in the ops bucket | `_descriptors_component`, `REVISION_PREFIX`, `publish` in `tools/olf/olf/infra/project_revision.py`; [ADR 0012](0012-project-revisions-and-promotion.md) **[V]** |
| A stage's active revision is found through `activations/<stage>/ACTIVE.json`, which names an `ACTIVATION.json` under `activations/<stage>/revisions/sha256/<digest>/`, which names the project revision | `active_key`, `activation_key`, `active` in `tools/olf/olf/infra/project_activation.py` **[V]** |
| Those readers live in `tools/olf` only; the platform API image carries `openlakeforge_core`, `openlakeforge_services` and `openlakeforge_platform_api` | `COPY` lines of `images/platform-api/Dockerfile` **[V]** |
| The evaluator's key `openlakeforge-alerts` holds `Read`, `Write` and `List` on `alerts/*` and `Read` and `List` on each stage's quality prefix, nothing else | `alerts_credentials` in `infra/terraform/modules/storage/seaweedfs/credentials.tf`; flow F39 in [ADR 0017](0017-local-network-policy-and-rbac.md) **[V]** |
| `olf init` copies the distribution's `lakehouse_code/` as the new project's template | `initialization.py` in `tools/olf/olf/common/` **[V]** |
| The alert e2e makes its run, missed-run and stale incidents on `gold_maintenance`, and its quality incidents on a product job | `tools/olf/olf/infra/e2e/_alerts.py` **[V]** |

So a product team cannot say, per product and per stage, what to watch, when it
fires or who is told. A sales team that wants its quality warnings and not the
platform on-call's pager has no field to write, and neither has a team that
needs a 26-hour freshness bound on one product.

## Decision 1: `lakehouse_code/alerts.yml`, an `AlertPolicy` descriptor

| Basis | Decision |
| --- | --- |
| Maintainer | A new project descriptor, `lakehouse_code/alerts.yml`, with `apiVersion: openlakeforge.io/v1beta1` and `kind: AlertPolicy`. |
| Maintainer | It follows ADR 0005: a closed JSON Schema at `docs/schema/alert-policy.schema.json` (written in slice 24-a; this record fixes its shape), a typed model in `openlakeforge_core`, both run by `olf check contracts`. |
| Maintainer | It is YAML, not Python: the evaluator reads it without importing project code. |
| Maintainer | It ships in the revision's `descriptors` component (Decision 6). |

The file is optional. A project without it gets no product alerts (Decision 3).

### Shape

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `routes` | map of route name to receiver name | yes, non-empty | Names the rules use for receivers. The receiver name is a `spec.observability.alerts.receivers[].name` of the profile (Decision 2). |
| `defaults.stages` | list of `dev`, `uat`, `prod` | no; every stage | Stages a rule applies to when it names none. |
| `defaults.notify` | list of route names | no | Routes a rule notifies when it names none. |
| `rules` | list, non-empty | yes | The rules below. |
| `rules[].match.domain` | a domain `name` of `governance.yml`, or `"*"` | yes | |
| `rules[].match.product` | a product `id` of `governance.yml`, or `"*"` | yes | |
| `rules[].stages` | list of stages | no; `defaults.stages` | |
| `rules[].notify` | list of route names | no; `defaults.notify` | A rule that resolves to no route is a validation error. |
| `rules[].when` | map of condition to options, or to `false` | yes, non-empty | `false` turns off a condition that a less specific rule turns on. |

Names follow the identifier pattern the profile's receiver names use. Unknown
fields at any level are rejected, as in every descriptor (ADR 0005). Matching
uses `governance.yml` ids: a product is matched through its job
`<id>_pipeline`, and its domain is the one it is nested under. A job that is no
product's (`gold_maintenance`, `silver_maintenance`) is matched only by a rule
whose `match` is `"*"` for both **(proposed)**.

### Conditions and their options

| `when` key | Alert kind | Options | Default |
| --- | --- | --- | --- |
| `run_failed` | `run_failed` | `severity`: `warning` or `critical` | `critical` |
| `missed_run` | `missed_run` | `grace`: a duration; `severity` | `15m` (today's `SCHEDULE_GRACE`); `critical` |
| `freshness` | `stale_output` | `warn_after`, `critical_after`: durations since the last successful run. With neither, cadence-derived staleness applies, with `grace` (default `15m`) and `severity` (default `critical`) | cadence-derived, as today |
| `quality` | `quality` | `on`: `warn` or `error` | `warn` |
| `schedule_mismatch` | `schedule_mismatch` | `severity` | `warning` |

`{}` turns a condition on with its defaults. A duration is a positive integer
with `m`, `h` or `d` (`30m`, `26h`, `2d`). `warn_after` must be below
`critical_after` when both are set; either may be set alone. Thresholds and
cadence options do not mix in one rule.

- **`freshness`** keeps the alert kind `stale_output` **(proposed)**, so
  fingerprints, payloads (`condition: stale_output`) and the guide's runbook
  anchors do not change. With thresholds, it opens at `warning` past
  `warn_after` and escalates to `critical` past `critical_after`, through the
  existing escalation rule (one `escalated` notification). It applies whatever
  the schedule's state **(proposed)**: a DEV pipeline has no schedule, and a
  declared bound is the team's statement that it should have run. A job with no
  success among the runs read (25, `RUN_WINDOW`) stays `unknown` and opens
  nothing, as today. Full SLAs, with business calendars and partitions, stay in
  #921.
- **`quality`** with `on: warn` alerts as today: a WARN or a non-blocking ERROR
  at `warning`, a blocking ERROR at `critical`. With `on: error`, a WARN alone
  does not alert. The signal says which outcome it saw, not only its state
  **[T: today the two warning cases differ only by their reason string; slice
  24-c carries the outcome on the signal instead of parsing text]**.
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
      quality: {on: error}

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
      quality: {on: warn}

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

What it resolves to for `order_revenue` in PROD (Decision 4):

| Condition | Winning rule | Options | Notifies |
| --- | --- | --- | --- |
| `run_failed` | global | `critical` | `on-call` |
| `missed_run` | `order_revenue` | grace `30m`, `critical` | `sales`, `on-call` |
| `freshness` | `order_revenue` | warn `26h`, critical `30h` | `sales`, `on-call` |
| `quality` | `sales` domain | `on: warn` | `sales` |
| `schedule_mismatch` | global | `warning` | `on-call` |

In DEV the only match is the second rule: a failed run notifies `data-eng` as a
warning, and nothing else alerts.

### What `olf check contracts` checks offline

The schema and the typed model; that every `notify` names a key of `routes`;
that every `match` names a domain and product of `governance.yml`, and that a
named product is nested under the named domain; and that no two rules of the
same specificity (Decision 4) set the same condition for an overlapping stage.
Stage names are checked against the vocabulary (`dev`, `uat`, `prod`), not
against a profile: the same revision activates under profiles that enable
different stages, and a rule for a stage that is not enabled is inert. Receiver
names cannot be checked here (Decision 2).

## Decision 2: receivers stay in the profile; the policy names them

| Basis | Decision |
| --- | --- |
| Maintainer | `openlakeforge.yaml` keeps the named receivers backed by Kubernetes Secrets, which the ops team owns. `alerts.yml` refers to them by name only, through `routes`. Credentials never enter the repository. |
| Maintainer | A route naming a receiver the profile does not declare fails activation, which has the profile. `olf check` validates only the structure. |
| Proposed | The check runs in `deploy_revision` (`tools/olf/olf/infra/deployment/activation.py`), beside `_require_source_bindings`, before the rollout, and only when the profile declares at least one receiver. With none, no evaluator exists (ADR 0023, Decision 1) and nothing reads the policy, so a project template that ships an `alerts.yml` still activates on a profile without receivers. |
| Proposed | The receiver's `stages` filter is **kept**, as a ceiling the ops team owns: a notification reaches a receiver only when a rule routes it there **and** the receiver's `stages` include the stage. A product rule cannot page a PROD-only on-call from DEV. Omitted `stages` resolves to every enabled stage, so the ceiling is open by default. |

The profile can change after a stage is activated. A route whose receiver has
since disappeared is skipped; the evaluator delivers to the routes that remain
and raises `policy_unreadable` for that stage (Decision 6).

## Decision 3: silent unless declared

| Basis | Decision |
| --- | --- |
| Maintainer | A product or stage that no rule matches does not alert. A project without `alerts.yml` gets no product alerts. |

This changes today's behaviour, in which every product's conditions alert to
every receiver. The migration:

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
   rule now covers resolves silently (Decision 5), so an upgrade does not send a
   burst of resolves.

Platform conditions are not product-scoped and are not affected (Decision 7).

## Decision 4: rule precedence

| Basis | Decision |
| --- | --- |
| Proposed | For each scope, stage and condition, the **most specific** matching rule wins: a named product, then a named domain with `product: "*"`, then the global `"*"`/`"*"`. A rule names a product whether or not it also names the domain. |
| Proposed | `notify` is the **winning rule's** only, not the union of every matching rule. |
| Proposed | Two rules at the same specificity that set the same condition for an overlapping stage are a validation error, never resolved by file order. |

The choice is per condition, so a product rule that sets only `freshness`
still takes `run_failed` from the domain or global rule.

Why the winner's `notify` and not the union: with a union, a broad rule can
never be narrowed. The example's sales quality warnings would always reach
`on-call` as well, because the global rule matches them; the only escape would
be removing the global rule for everyone. With the winner's list, a team widens
by naming both routes (`[sales, on-call]`) and narrows by naming one. Every
notification is explained by exactly one rule, which `olf alert explain`
(Decision 9) can print. The cost is repetition: a product rule that wants the
on-call too must say so.

`false` in `when` wins like any value, which is how a product opts out of a
condition a domain rule turns on.

## Decision 5: incident state under a changing policy

Incident identity stays `fingerprint(scope, kind, component)` (ADR 0023,
Decision 3), and the scope keeps the job as `product`. The policy is not part of
the fingerprint, so editing a rule never turns one incident into two.

| Policy change while an incident is open | Effect |
| --- | --- |
| The condition stops being declared for the scope and stage (rule removed, `false`, stage dropped, `alerts.yml` removed) | The incident resolves **silently**: `resolved_silently` in history, no notification. Its signals are ignored from then on. This uses the mechanism that already closes the incidents of a removed product (`active_scopes` in `alerts.evaluate`) **[V]** |
| Severity rises (`severity` option, or a freshness threshold crossed) | Re-evaluated on the next run: one `escalated` notification, as today |
| Severity falls | No notification; history records it, as today |
| Routes change | Routing is computed when a notification is sent, as today (`run_once` recomputes the routed set every attempt **[V]**). An open incident's later `resolved` goes to the routes of the rule that wins then. A queued notification whose new routes are all delivered or empty leaves the outbox |
| A threshold or grace changes | Applies from the next evaluation; an incident whose condition no longer holds resolves with a notification |

## Decision 6: how the evaluator gets the policy

| Basis | Decision |
| --- | --- |
| Maintainer | `alerts.yml` is an entry of the revision's `descriptors` component. Each evaluation reads `activations/<stage>/ACTIVE.json`, then that revision's `alerts.yml` from the ops bucket. |
| Maintainer | This needs a new read grant on the `openlakeforge-alerts` key. |
| Proposed | The evaluator also reads the same revision's `governance.yml`, to map jobs to product ids and domains, and the revision's sidecar, to check both files' sha256 against the manifest. |
| Proposed | A stage with no activation, or whose revision has no `alerts.yml`, has an empty policy, and no warning: both are normal states. |
| Proposed | A stage whose policy cannot be used keeps its **last good policy** and raises a stage-scoped `policy_unreadable` warning to the platform receivers (Decision 7). |

The read path, per stage and evaluation:

1. `activations/<stage>/ACTIVE.json`. When its activation revision equals the
   one cached for the stage, the cached policy is used and the path stops here.
2. `activations/<stage>/revisions/sha256/<digest>/ACTIVATION.json`, for the
   project revision.
3. `project/revisions/sha256/<digest>/PROJECT-REVISION.json`, then
   `descriptors/lakehouse_code/alerts.yml` and
   `descriptors/lakehouse_code/governance.yml` under the same prefix.

The pointer, activation and sidecar readers move from `tools/olf` into
`openlakeforge_core` in slice 24-b, because the evaluator image does not carry
`olf` **[V]**; `olf` then imports them.

The last good policy is kept per stage at `alerts/state/v1/policy/<stage>.json`
(the activation revision, the validated policy and the job map), inside the
existing `alerts/*` grant. `policy_unreadable` fires when a read fails, a digest
does not match, the file fails validation (a revision built by a newer
distribution) or a route names a receiver the profile no longer has. It is a
`warning`, and resolves on the first evaluation that reads the stage's policy
cleanly. With no last good policy at all (the first evaluation after a deploy),
the stage evaluates with an empty policy and still raises the warning.

### The new grant

`Read` on `activations/<stage>/ACTIVE.json` and
`activations/<stage>/revisions/*` for each enabled stage, and `Read` on
`project/revisions/*`. No `List` and no `Write`. It is a row change in
`credentials.tf` and a text change to flow F39 in ADR 0017; no new flow, since
the evaluator already reaches SeaweedFS on 8333.

The ADR 0017 implication: `project/revisions/*` holds every frozen file of
every revision (dbt SQL, Dagster code, Floe contracts), so the key reads more
than the two files it needs. The content carries no credential by construction,
and the value scan rejects concrete secrets before publication (ADR 0012), but
the key's reach is wider than its use **[T: whether SeaweedFS honours a
wildcard in the middle of an action path, which would narrow it to
`project/revisions/sha256/*/descriptors/*`]**. See open question 4.

## Decision 7: platform conditions stay with the profile

`stage_unreadable`, the new `policy_unreadable`, and the log archive's delivery
signals if they ever become conditions, describe the platform, not a product.
The log archive signals (`log-archive.spool-age`, `log-archive.spool-size`,
`log-archive.newest-object`) are `service` signals of the stage health route and
are not an alert source today: `_kinds_of` maps a `service` signal only for the
two Dagster read components **[V]**.

| Basis | Decision |
| --- | --- |
| Proposed | `alerts.yml` cannot name a platform condition; the schema rejects them as unknown `when` keys. |
| Proposed | They go to the profile's platform receivers: a new optional `spec.observability.alerts.platform_receivers`, a list of receiver names. Omitted means every receiver, which is today's behaviour. Each receiver's `stages` ceiling still applies. |

The product team owns `alerts.yml`; the ops team owns the cluster, the
receivers and the Secrets. A Dagster that cannot be read, or a policy the
evaluator cannot use, is the ops team's problem whatever the product, and
routing it from a file the ops team does not own would let one bad revision
silence the platform's own alarms. **Flagged for the maintainer**: the profile
field is a schema change to `DeploymentProfile`, which ADR 0011 owns.

## Decision 8: what changes in the merged code

| File | Change |
| --- | --- |
| `packages/core/openlakeforge_core/alert_policy.py` (new) | `AlertPolicy`, `AlertRule`, condition options; `load_alert_policy`, `validate_alert_policy`; `resolve_policy(policy, governance, stage) -> ResolvedPolicy`, which answers per job and condition: enabled, options, route names |
| `packages/core/openlakeforge_core/alerts.py` | `evaluate(previous, signals, now, *, policy: ResolvedPolicy, ...)`. A signal whose condition the policy does not enable for its scope is ignored, and its open incident resolves silently. `_severity_of` takes the severity from the options instead of constants; `quality` applies `on`; `stale_output` can open at `warning` |
| `packages/core/openlakeforge_core/health.py` | `evaluate_freshness` gains an age mode: `warn_after` and `critical_after`, measured from the last success's end, for any schedule state |
| `packages/platform-api/openlakeforge_platform_api/pipeline_health.py` | `read_stage_pipelines` and `_evaluate` take per-job grace and thresholds from the resolved policy. `SCHEDULE_GRACE` becomes the default grace and `RUN_MAX_AGE` stays the bound for a job with no running schedule. The health routes call it without a policy, so they keep today's cadence-derived signals **(proposed)** |
| `packages/platform-api/openlakeforge_platform_api/alerting/run.py` | Reads each stage's policy (Decision 6) before evaluating. Routing replaces `receiver.routes(stage)` with the winning rule's routes, mapped to receivers and capped by each receiver's `stages`; platform conditions route to the platform receivers |
| `packages/platform-api/openlakeforge_platform_api/alerting/settings.py` | `ReceiverSetting.stages` is kept as the ceiling. New: the ops bucket's activation and revision prefixes per stage, and `platform_receivers` |
| `tools/olf/olf/infra/project_revision.py` | `_descriptors_component` adds `alerts.yml` when it exists; the component is a path map, so the revision schema does not change **[V: `ComponentEntries.entries`]** |
| `tools/olf/olf/infra/deployment/activation.py` | The route check of Decision 2 |
| `tools/olf/olf/common/profile.py` | `platform_receivers`, if Decision 7 is accepted. The receiver `stages` filter is kept, not deprecated |
| `infra/terraform/modules/storage/seaweedfs/credentials.tf` | The read grant of Decision 6 |

ADR 0023 keeps evaluation, state, delivery, links, network and retention.
Its Decision 2 table becomes the defaults of a declared condition, and its
Decision 4 "Stage routing" row becomes the ceiling described in Decision 2
above. Its record carries a status note and a History line saying so. ADR 0005
gains `AlertPolicy` as a third descriptor in slice 24-a, and ADR 0012's
`descriptors` row gains `alerts.yml` in slice 24-b.

## Decision 9: not Dagster's own alerting

| Basis | Decision |
| --- | --- |
| Proposed | The policy is evaluated by the ADR 0023 evaluator, not expressed as Dagster freshness policies, asset checks or sensors. |

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
   `tools/olf/olf/common/project_code_check.py` **[V]**), and ADR 0023,
   Decision 1 rejected a sensor because it shares fate with the scheduler it
   watches.
3. **The evaluator must not import project code.** Dagster policies are Python
   objects inside the code location; reading them back needs the stage's
   Dagster to answer, which is the outage `stage_unreadable` exists for.

Floe and dbt keep their own thresholds: the quality manifest carries their
outcome, and `quality.on` only chooses which outcome alerts.

An `olf alert explain --stage <stage> --product <id>` command prints the
winning rule, options and routes per condition, offline, from `alerts.yml` and
`governance.yml` **(proposed)**. It needs no credential, so it is top-level
under ADR 0020.

## Consequences

- A product team changes what alerts, when and to whom by a pull request to
  `alerts.yml`, reviewed with the code it watches, and promoted with the same
  revision. DEV and PROD differ inside one file.
- The ops team keeps the receivers, their credentials and a per-receiver stage
  ceiling; a revision can never add a webhook.
- Upgrading is a behaviour change: a project must ship `alerts.yml` to keep
  product alerts.
- The evaluator key reads project revisions, a wider reach than ADR 0023 gave
  it.
- A policy error in one stage degrades that stage to its last good policy and
  one warning, never to silence across stages.
- The portal and the health routes keep the cadence-derived view; a team's
  freshness thresholds show up only as alerts until the routes read the policy.

## Open questions

Each has a recommended answer, which this record has used above.

1. **`notify`: the winner's or the union?** Recommended: the winning rule's
   only (Decision 4).
2. **Platform conditions.** Recommended: routed by a new profile field
   `spec.observability.alerts.platform_receivers`, every receiver when omitted;
   never by `alerts.yml` (Decision 7).
3. **An unusable policy.** Recommended: keep the last good policy and raise
   `policy_unreadable` (`warning`) to the platform receivers; an absent
   activation or an absent `alerts.yml` is an empty policy with no warning
   (Decision 6).
4. **The width of the read grant.** Recommended: accept `Read` on
   `project/revisions/*`, and narrow it in slice 24-b if SeaweedFS honours a
   mid-path wildcard. The alternative, activation copying the two files under
   `activations/<stage>/`, contradicts the decided read path.
5. **The receiver `stages` filter.** Recommended: keep it as an ops-owned
   ceiling rather than deprecate it (Decision 2).
6. **Jobs of no product.** Recommended: only the global `"*"`/`"*"` rule
   matches `gold_maintenance` and `silver_maintenance`. The alert e2e runs on
   `gold_maintenance`, so this keeps it working.
7. **The `freshness` key and the `stale_output` kind.** Recommended: keep the
   kind, so fingerprints, payloads and runbook anchors do not change. Renaming
   the kind is a payload change for every receiver.
8. **Thresholds without a running schedule.** Recommended: a declared
   `warn_after` or `critical_after` applies in any schedule state; a team that
   stops a pipeline on purpose changes the rule.
9. **The health routes and the policy.** Recommended: the routes stay
   policy-free in v0.7.
10. **The activation route check without receivers.** Recommended: skipped when
    the profile has no receiver, so the sample project activates everywhere.
11. **An e2e flag for the policy.** Recommended: a `--policy-file` flag, for
    the e2e only, beside `--now` and `--state-prefix` (ADR 0023, Decision 3),
    so slice 24-c can test silence and precedence without building a revision
    per case.

## Build order

Each slice stays under the 15-file, 600-line cap in AGENTS.md.

| Slice | Content | Updates |
| --- | --- | --- |
| 24-a | `docs/schema/alert-policy.schema.json`; `openlakeforge_core/alert_policy.py` with the precedence resolver; `olf check contracts`; `olf alert explain`; the sample project's `alerts.yml`. No runtime reads it yet | ADR 0005; `docs/reference/descriptors.md` |
| 24-b | `alerts.yml` in the `descriptors` component; the pointer, activation and sidecar readers in core; the evaluator's read path, last good policy and `policy_unreadable`; the read grant; the activation route check. The evaluator reads the policy and does not apply it yet | ADR 0012; ADR 0017 (F39) |
| 24-c | `alerts.evaluate` takes the resolved policy; options, thresholds and routing; `pipeline_health` thresholds; `platform_receivers`, if accepted; the conformance profile's receiver rename; the e2e cases for silence, precedence and thresholds | `docs/reference/deployment-profile.md` |
| 24-d | The alerts guide (the migration callout, the policy, each condition's options), the policy reference, the health-signals page | `docs/guides/operations/alerts.md`, `docs/reference/` |

Slice 24-c is where behaviour changes, so the sample policy (24-a), the read
path (24-b) and the migration land before or with it.

## Rejected

| Option | Why not |
| --- | --- |
| Rules in Python inside the code location | The evaluator would have to import project code (Decision 1, maintainer) |
| Receivers declared in `alerts.yml` | Credentials and Secrets are the ops team's; the revision would carry deployment intent (ADR 0012) |
| One policy file per stage | One revision serves every stage (ADR 0012); stage differences live in `stages` (maintainer) |
| Policy in `openlakeforge.yaml` | Product teams would edit the ops team's profile, and the policy would not be reviewed with the product code |
| Alert by default, opt out per product | Maintainer: silent unless declared |
| Dagster freshness policies, asset checks or sensors | Decision 9 |

## History

2026-11-17: created. Takes over condition selection, thresholds and routing
from ADR 0023.
