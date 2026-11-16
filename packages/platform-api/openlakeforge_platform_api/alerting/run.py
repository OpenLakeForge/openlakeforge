"""One evaluation: read state, evaluate, write state, deliver, record outcomes, expire (ADR 0023, decision 3).

The state, outbox included, is written before anything is delivered. A crash after that write sends the same
notification again under the same idempotency key on the next run: at-least-once, never silence. A store that cannot
be read or written raises `StoreError` and nothing is delivered, because delivering what cannot be recorded would
repeat every notification every run.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from openlakeforge_core.alerts import DAGSTER_READ_API, AlertState, HistoryEvent, Incident, evaluate
from openlakeforge_core.alerts_state import drop_notification, expire_outbox, record_attempt, record_delivery
from openlakeforge_core.health import HealthState, Scope, Signal, service_signal
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.notify import Notifier

from openlakeforge_platform_api.alerting.settings import EvaluatorSettings, LinkSettings
from openlakeforge_platform_api.alerting.store import AlertStore
from openlakeforge_platform_api.pipeline_health import read_stage_pipelines

log = logging.getLogger(__name__)

HISTORY_SCHEMA = "openlakeforge.alert_history.v1"
# The Dagster read signal is observed now and judged now; the bound only has to be positive.
_READ_SIGNAL_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class RunSummary:
    notifications: int
    delivered: int
    failed: int
    expired: int


def run_once(
    *,
    settings: EvaluatorSettings,
    store: AlertStore,
    dagster: Mapping[str, DagsterClient],
    notifiers: Mapping[str, Notifier],
    now: datetime,
) -> RunSummary:
    now = now.astimezone(UTC)
    previous = store.read_state(now)
    signals, scopes = _read_stages(settings, dagster, previous, now)
    evaluation = evaluate(
        previous,
        signals,
        now,
        links=lambda incident: _links(settings.links, incident),
        active_stages=settings.stages,
        active_scopes=scopes,
    )
    state = evaluation.state
    # History first: its keys are deterministic, so a rewrite after a failed state write overwrites rather than
    # duplicates, while history written after the state would be lost for good on a crash in between (a silent
    # resolve has no outbox entry to recover it from). A failure here leaves the state as it was, so the next run
    # sees the same transitions again.
    for event in evaluation.events:
        store.write_history(now, f"{event.incident.id}-{event.event.value}", _event_record(event))
    store.write_state(state)

    delivered = failed = 0
    for entry in state.outbox:
        notification = entry.notification
        routed = {receiver.name for receiver in settings.receivers if receiver.routes(notification.scope.stage)}
        if not routed:
            state = drop_notification(state, notification.id)
            continue
        for name in sorted(routed - entry.delivered):
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
                    receiver.name
                    for receiver in settings.receivers
                    if receiver.routes(entry.notification.scope.stage) and receiver.name not in entry.delivered
                ),
                "attempts": dict(sorted(entry.attempts.items())),
                "runs": entry.runs,
                "at": now.isoformat(),
            },
        )
    store.write_state(state)
    return RunSummary(len(evaluation.notifications), delivered, failed, len(expired))


def _read_stages(
    settings: EvaluatorSettings, dagster: Mapping[str, DagsterClient], previous: AlertState, now: datetime
) -> tuple[list[Signal], list[Scope]]:
    """Signals of every enabled stage, and the products that still exist in the stages that could be read.

    An unreadable stage, or one listing no job at all, keeps the scopes of its open incidents: they are held, not
    resolved as deleted.
    """
    signals: list[Signal] = []
    scopes: list[Scope] = []
    for stage in settings.stages:
        status, pipelines = read_stage_pipelines(dagster.get(stage), stage, now)
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
