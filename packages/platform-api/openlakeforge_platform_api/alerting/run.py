"""One evaluation: read state, evaluate, write state, deliver, record outcomes, expire (ADR 0023, decision 3).

The state, outbox included, is written before anything is delivered, and again after every send attempt. A crash
after the first write sends the same notification again under the same idempotency key on the next run:
at-least-once, never silence; the per-attempt writes bound that repeat to the one send in flight, so a receiver that
already has the notification is not sent it again. A store that cannot
be read or written raises `StoreError` and nothing is delivered, because delivering what cannot be recorded would
repeat every notification every run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from openlakeforge_core.alert_policy import AlertPolicy, ResolvedPolicy, resolve_policy
from openlakeforge_core.alerts import ALERT_POLICY, DAGSTER_READ_API, AlertState, HistoryEvent, Incident, evaluate
from openlakeforge_core.alerts_state import drop_notification, expire_outbox, record_attempt, record_delivery
from openlakeforge_core.health import HealthState, Scope, Signal, service_signal
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.notify import Notifier

from openlakeforge_platform_api.alerting.policy import StagePolicy, read_stage_policy
from openlakeforge_platform_api.alerting.routing import routed_receivers
from openlakeforge_platform_api.alerting.settings import EvaluatorSettings, LinkSettings
from openlakeforge_platform_api.alerting.store import AlertStore, PendingRecord
from openlakeforge_platform_api.pipeline_health import read_stage_pipelines
from openlakeforge_platform_api.quality_health import QualityReader

log = logging.getLogger(__name__)

HISTORY_SCHEMA = "openlakeforge.alert_history.v1"
# The Dagster read signal is observed now and judged now; the bound only has to be positive.
_READ_SIGNAL_MAX_AGE = timedelta(minutes=5)
# Stops starting sends well inside the CronJob's `active_deadline_seconds` (240, infra/terraform/modules/observability/
# alerts/main.tf): one send can take about 65 s (`notify.py`), so the last one begun at the budget still ends before
# the deadline kills the pod. Keep the two in step.
DELIVERY_BUDGET_SECONDS = 150.0


@dataclass(frozen=True)
class RunSummary:
    notifications: int
    delivered: int
    failed: int
    expired: int
    # Stages whose alert policy could not be used this run.
    policy_unreadable: tuple[str, ...] = ()


def run_once(
    *,
    settings: EvaluatorSettings,
    store: AlertStore,
    dagster: Mapping[str, DagsterClient],
    notifiers: Mapping[str, Notifier],
    now: datetime,
    quality: Mapping[str, QualityReader] | None = None,
    policy_override: AlertPolicy | None = None,
    delivery_budget: float = DELIVERY_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> RunSummary:
    started = monotonic()
    now = now.astimezone(UTC)
    previous = store.read_state(now)
    # Transitions of the stored state whose history writes did not all land last run: replay them now.
    _flush_history(store, store.read_pending_history(previous))
    policy_unreadable, stage_policies = _read_policies(settings, store, now, policy_override)
    policy = {stage: found.resolved for stage, found in stage_policies.items() if _usable(found)}
    signals, scopes = _read_stages(settings, dagster, quality or {}, previous, now, policy)
    signals += [_policy_signal(stage, stage in policy_unreadable, now) for stage in settings.stages]
    evaluation = evaluate(
        previous,
        signals,
        now,
        policy=policy,
        links=lambda incident: _links(settings.links, incident),
        active_stages=settings.stages,
        active_scopes=scopes,
    )
    state = evaluation.state
    # The transitions are first saved as pending, tagged with the state they belong to, then the state, then the
    # history. A crash after the state write replays them next run (a silent resolve has no outbox entry to
    # recover it from); a failed state write leaves them tagged with a state that never committed, so they are
    # dropped instead of recording incident ids the next evaluation will mint afresh. Each record is filed under
    # its own event's day, so a replay lands where the first attempt would have.
    pending = [
        (f"{event.event_at.astimezone(UTC):%Y-%m-%d}", f"{event.incident.id}-{event.event.value}", _event_record(event))
        for event in evaluation.events
    ]
    if pending:
        store.write_pending_history(pending, state)
    store.write_state(state, evaluated_at=now)
    _flush_history(store, pending)

    delivered = failed = 0
    out_of_time = False
    for entry in state.outbox:
        if out_of_time:
            break
        notification = entry.notification
        routed = routed_receivers(notification, settings, stage_policies)
        if routed is None:
            # No policy could be read to route it by. It stays queued, and its `runs` count, already advanced by this
            # run's evaluation, still expires it.
            continue
        if not routed or routed <= entry.delivered:
            # Nobody to send to, or every receiver still routed to it has it (a route can change between runs).
            state = drop_notification(state, notification.id)
            continue
        for name in sorted(routed - entry.delivered):
            if monotonic() - started >= delivery_budget:
                # What is left stays queued for the next run. Its `runs` counter was already advanced by this
                # run's evaluation, so a starved entry still reaches `expire_outbox` rather than waiting forever.
                log.warning("delivery budget of %.0fs spent; the rest stays queued", delivery_budget)
                out_of_time = True
                break
            state = record_attempt(state, notification.id, name)
            result = notifiers[name].send(notification.payload(), delivery_id=notification.id)
            attempt = next(e for e in state.outbox if e.notification.id == notification.id).attempts[name]
            store.write_history(
                now,
                f"{notification.incident_id}-{notification.event.value}-delivery-{name}-{attempt}",
                {
                    "schema": HISTORY_SCHEMA,
                    "record": "delivery",
                    "notification_id": notification.id,
                    "receiver": name,
                    "outcome": result.status,
                    "http_status": result.http_status,
                    "attempts": result.attempts,
                    "at": now.isoformat(),
                },
            )
            if result.status == "delivered":
                state = record_delivery(state, notification.id, name, routed=routed)
                delivered += 1
            elif result.status == "rejected":
                # A 4xx other than 408/429 will not change on retry (ADR 0023, decision 4): settle this receiver so the
                # next runs do not resend it; the history record above keeps the real outcome, `rejected`.
                state = record_delivery(state, notification.id, name, routed=routed)
                failed += 1
            else:
                failed += 1
            # Saved per attempt: a pod killed at its deadline keeps what it delivered, so the next run neither
            # resends it nor lets the outbox stall short of expiry.
            store.write_state(state, evaluated_at=now)

    state, expired = expire_outbox(state)
    for entry in expired:
        log.warning("notification %s is undeliverable after %d runs", entry.notification.id, entry.runs)
        store.write_history(
            now,
            f"{entry.notification.incident_id}-{entry.notification.event.value}-undeliverable",
            {
                "schema": HISTORY_SCHEMA,
                "record": "undeliverable",
                "notification_id": entry.notification.id,
                "receivers": sorted(
                    (routed_receivers(entry.notification, settings, stage_policies) or set()) - entry.delivered
                ),
                "attempts": dict(sorted(entry.attempts.items())),
                "runs": entry.runs,
                "at": now.isoformat(),
            },
        )
    store.write_state(state, evaluated_at=now)
    return RunSummary(len(evaluation.notifications), delivered, failed, len(expired), policy_unreadable)


def _read_policies(
    settings: EvaluatorSettings, store: AlertStore, now: datetime, override: AlertPolicy | None = None
) -> tuple[tuple[str, ...], dict[str, StagePolicy]]:
    """Every stage's policy (ADR 0023, decision 10), and the stages whose policy is not fully usable.

    `override` is the e2e's `--policy-file`: it replaces every stage's policy, matched against the governance of the
    revision the stage is running, and nothing about it can be unreadable."""
    names = [receiver.name for receiver in settings.receivers]
    unreadable = []
    found: dict[str, StagePolicy] = {}
    for stage in settings.stages:
        found[stage] = read_stage_policy(stage, store, now, receivers=names)
        if override is not None:
            governance = found[stage].governance or {"domains": []}
            found[stage] = StagePolicy(
                stage, override, resolve_policy(override, governance, stage), found[stage].revision or "override"
            )
        if found[stage].unreadable:
            unreadable.append(stage)
        else:
            log.info("alert policy of stage %s: %s", stage, found[stage].revision or "none")
    return tuple(unreadable), found


def _usable(found: StagePolicy) -> bool:
    """A stage with no policy at all, never read and not readable now, is left out of `evaluate` so its open incidents
    are held: an object store hiccup on the first read after the cache was lost must not resolve what is firing."""
    return found.revision is not None or not found.unreadable


def _policy_signal(stage: str, unreadable: bool, now: datetime) -> Signal:
    return service_signal(
        Scope(stage=stage),
        ALERT_POLICY,
        HealthState.WARNING if unreadable else HealthState.HEALTHY,
        "alert policy could not be used" if unreadable else "alert policy read cleanly",
        now=now,
        max_age=_READ_SIGNAL_MAX_AGE,
    )


def _flush_history(store: AlertStore, pending: list[PendingRecord]) -> None:
    if not pending:
        return
    for day, name, record in pending:
        store.write_history_at(day, name, record)
    store.write_pending_history([], None)


def _read_stages(
    settings: EvaluatorSettings,
    dagster: Mapping[str, DagsterClient],
    quality: Mapping[str, QualityReader],
    previous: AlertState,
    now: datetime,
    policy: Mapping[str, ResolvedPolicy],
) -> tuple[list[Signal], list[Scope]]:
    """Signals of every enabled stage, and the products that still exist in the stages that could be read.

    An unreadable stage, or one listing no job at all, keeps the scopes of its open incidents: they are held, not
    resolved as deleted.
    """
    signals: list[Signal] = []
    scopes: list[Scope] = []
    for stage in settings.stages:
        status, pipelines = read_stage_pipelines(dagster.get(stage), stage, now, quality.get(stage), policy.get(stage))
        readable = status.status == "available"
        # Exactly one per stage and evaluation: a stage with none would have its unreadable counter reset.
        signals.append(
            service_signal(
                Scope(stage=stage),
                DAGSTER_READ_API,
                HealthState.HEALTHY if readable else HealthState.UNKNOWN,
                status.reason,
                now=now,
                max_age=_READ_SIGNAL_MAX_AGE,
            )
        )
        for pipeline in pipelines:
            signals.extend(pipeline.signals)
            scopes.append(Scope(stage=stage, product=pipeline.job))
        # An unreadable stage, or one whose Dagster answers with no job at all (a code location not loaded yet, as
        # the service probe reports it), is no evidence that a product was removed: keep its open incidents' scopes.
        # A product missing while other jobs are listed is a real removal and resolves.
        if not readable or not pipelines:
            scopes.extend(i.scope for i in previous.incidents if i.scope.stage == stage and i.scope.product)
    return signals, scopes


def _links(links: LinkSettings, incident: Incident) -> dict[str, str]:
    found: dict[str, str] = {}
    if links.dagster_host_template and incident.run_ids:
        host = links.dagster_host_template.format(stage=incident.scope.stage)
        found["run"] = f"{links.dagster_scheme}://{host}/runs/{incident.run_ids[-1]}"
    if links.runbook_base_url:
        found["runbook"] = f"{links.runbook_base_url}#{incident.kind.value}"
    return found


def _event_record(event: HistoryEvent) -> dict[str, Any]:
    incident = event.incident
    scope = incident.scope
    return {
        "schema": HISTORY_SCHEMA,
        "record": "event",
        "event": event.event.value,
        "incident_id": incident.id,
        "condition": incident.kind.value,
        "severity": incident.severity.value,
        "stage": scope.stage,
        "domain": scope.domain,
        "product": scope.product,
        "table": scope.table,
        "run_id": event.run_id,
        "at": event.event_at.isoformat(),
    }
