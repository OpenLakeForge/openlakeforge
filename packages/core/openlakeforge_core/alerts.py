"""Provider-neutral alert incidents: turn health signals into at most one notification per transition.

Consumers pass in the signals they classified and the state they persisted last time; nothing here performs
I/O, so the same previous state, signals and `now` always give the same new state and notifications.

Product rules (maintainer decisions for v0.7): incidents notify once when they open and once when they
resolve; there is no acknowledgement and there are no reminders; a warning that escalates to critical
notifies once more; a resolve caused by a disabled pipeline is silent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .health import HealthState, Scope, Signal, SignalKind

STATE_SCHEMA_VERSION = 1
PAYLOAD_VERSION = 1
FINGERPRINT_VERSION = "1"
# Unknown evaluations in a row before an unreadable Dagster becomes an incident of its own.
UNREADABLE_THRESHOLD = 3
DAGSTER_COMPONENT = "dagster"
# Bounds the state document: a pipeline failing every five minutes must not grow its incident forever.
MAX_RUN_IDS = 20
DEFAULT_RESOLVED_RETENTION = timedelta(days=7)


class AlertKind(StrEnum):
    RUN_FAILED = "run_failed"
    MISSED_RUN = "missed_run"
    SCHEDULE_MISMATCH = "schedule_mismatch"
    STALE = "stale"
    QUALITY = "quality"
    STAGE_UNREADABLE = "stage_unreadable"


class AlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentState(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class AlertEvent(StrEnum):
    OPENED = "opened"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class AlertStateError(ValueError):
    """The persisted alert state cannot be read; the caller decides whether to keep a copy and start over."""


# The only free text a notification may carry. Signal reasons from `health.py` for workloads embed counts
# and a run's own text could carry anything, so a reason outside this set is replaced by the kind's default.
_REASONS = frozenset(
    {
        "latest run failed",
        "an expected run did not start",
        "quality checks warned",
        "a blocking quality check failed",
        "a non-blocking quality check failed",
        "data is stale",
        "schedule does not match its descriptor",
        "stage could not be read",
        "recovered",
    }
)
_DEFAULT_REASON = {
    AlertKind.RUN_FAILED: "latest run failed",
    AlertKind.MISSED_RUN: "an expected run did not start",
    AlertKind.SCHEDULE_MISMATCH: "schedule does not match its descriptor",
    AlertKind.STALE: "data is stale",
    AlertKind.QUALITY: "a blocking quality check failed",
    AlertKind.STAGE_UNREADABLE: "stage could not be read",
}
_RECOVERED = "recovered"


@dataclass(frozen=True)
class Incident:
    id: str
    fingerprint: str
    kind: AlertKind
    severity: AlertSeverity
    scope: Scope
    opened_at: datetime
    last_seen_at: datetime
    reason: str
    state: IncidentState = IncidentState.FIRING
    component: str | None = None
    run_ids: tuple[str, ...] = ()
    resolved_at: datetime | None = None


@dataclass(frozen=True)
class Notification:
    """A change worth telling someone about. Every field is on the payload allowlist."""

    id: str
    event: AlertEvent
    incident_id: str
    severity: AlertSeverity
    kind: AlertKind
    scope: Scope
    reason: str
    occurred_at: datetime
    opened_at: datetime
    run_id: str | None = None
    links: tuple[tuple[str, str], ...] = ()

    def payload(self) -> dict[str, Any]:
        """The webhook body. Built field by field: nothing that is not listed here can reach a receiver."""
        return {
            "version": PAYLOAD_VERSION,
            "event": self.event.value,
            "incident_id": self.incident_id,
            "severity": self.severity.value,
            "kind": self.kind.value,
            "stage": self.scope.stage,
            "domain": self.scope.domain,
            "product": self.scope.product,
            "table": self.scope.table,
            "run_id": self.run_id,
            "reason": self.reason,
            "opened_at": self.opened_at.isoformat(),
            "occurred_at": self.occurred_at.isoformat(),
            "links": dict(self.links),
        }


@dataclass(frozen=True)
class OutboxEntry:
    notification: Notification
    # Delivery attempts made per receiver name; the sender owns the retry policy.
    attempts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertState:
    incidents: tuple[Incident, ...] = ()
    outbox: tuple[OutboxEntry, ...] = ()
    # Consecutive unknown evaluations per fingerprint, kept only while above zero.
    unknown_counts: Mapping[str, int] = field(default_factory=dict)

    def firing(self) -> tuple[Incident, ...]:
        return tuple(incident for incident in self.incidents if incident.state is IncidentState.FIRING)

    def to_json(self) -> str:
        return json.dumps(_state_to_dict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> AlertState:
        try:
            document = json.loads(text)
        except ValueError as error:
            raise AlertStateError(f"alert state is not valid JSON: {error}") from error
        return _state_from_dict(document)


def fingerprint(scope: Scope, kind: AlertKind, component: str | None = None) -> str:
    """Identity of an incident. The run id is deliberately absent: repeated failures are one incident."""
    parts = (FINGERPRINT_VERSION, scope.stage, kind.value, scope.domain, scope.product, scope.table, component)
    material = "\x1f".join("\x00" if part is None else part for part in parts)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def evaluate(
    previous: AlertState,
    signals: Iterable[Signal],
    now: datetime,
    *,
    links: Callable[[Incident], Mapping[str, str]] | None = None,
    resolved_retention: timedelta = DEFAULT_RESOLVED_RETENTION,
    unreadable_threshold: int = UNREADABLE_THRESHOLD,
) -> tuple[AlertState, list[Notification]]:
    """Advance the incidents by one evaluation. A fingerprint with no signal this time is left as it was."""
    if now.tzinfo is None:
        raise ValueError("alert timestamps must be timezone-aware")
    observed = _classify(signals)
    by_fingerprint = {incident.fingerprint: incident for incident in previous.incidents if _is_firing(incident)}
    finished = [incident for incident in previous.incidents if not _is_firing(incident)]
    unknown_counts = dict(previous.unknown_counts)
    notifications: list[Notification] = []
    incidents = dict(by_fingerprint)

    for key, item in observed.items():
        current = incidents.get(key)
        if item.kind is AlertKind.STAGE_UNREADABLE:
            if item.health is HealthState.UNKNOWN:
                unknown_counts[key] = unknown_counts.get(key, 0) + 1
                if unknown_counts[key] >= unreadable_threshold:
                    item = replace(item, health=HealthState.CRITICAL, reason=_DEFAULT_REASON[item.kind])
            else:
                unknown_counts.pop(key, None)
        if item.health in (HealthState.WARNING, HealthState.CRITICAL) and item.severity is not None:
            if current is None:
                current = _open(item, now)
                incidents[key] = current
                notifications.append(_notify(AlertEvent.OPENED, current, now, item.run_id, links))
                continue
            updated = replace(
                current,
                last_seen_at=now,
                run_ids=_remember(current.run_ids, item.run_id),
                severity=item.severity,
            )
            incidents[key] = updated
            if current.severity is AlertSeverity.WARNING and item.severity is AlertSeverity.CRITICAL:
                notifications.append(_notify(AlertEvent.ESCALATED, updated, now, item.run_id, links))
        elif current is not None and item.health is HealthState.HEALTHY:
            resolved = replace(current, state=IncidentState.RESOLVED, resolved_at=now, last_seen_at=now)
            finished.append(resolved)
            del incidents[key]
            notifications.append(_notify(AlertEvent.RESOLVED, resolved, now, None, links, reason=_RECOVERED))
        elif current is not None and item.health is HealthState.DISABLED:
            finished.append(replace(current, state=IncidentState.RESOLVED, resolved_at=now))
            del incidents[key]

    kept = [
        incident
        for incident in finished
        if incident.resolved_at is None or now < incident.resolved_at + resolved_retention
    ]
    state = AlertState(
        incidents=tuple(sorted([*incidents.values(), *kept], key=lambda incident: (incident.opened_at, incident.id))),
        outbox=(*previous.outbox, *(OutboxEntry(notification) for notification in notifications)),
        unknown_counts={key: count for key, count in sorted(unknown_counts.items()) if count > 0},
    )
    return state, notifications


def record_attempt(state: AlertState, notification_id: str, receiver: str) -> AlertState:
    """Count one delivery attempt of a queued notification to a receiver."""
    entries = tuple(
        replace(entry, attempts={**entry.attempts, receiver: entry.attempts.get(receiver, 0) + 1})
        if entry.notification.id == notification_id
        else entry
        for entry in state.outbox
    )
    return replace(state, outbox=entries)


def drop_notification(state: AlertState, notification_id: str) -> AlertState:
    """Remove a notification once every receiver has it or the sender has given up."""
    return replace(state, outbox=tuple(entry for entry in state.outbox if entry.notification.id != notification_id))


@dataclass(frozen=True)
class _Observed:
    kind: AlertKind
    scope: Scope
    component: str | None
    health: HealthState
    severity: AlertSeverity | None
    reason: str
    run_id: str | None
    fingerprint: str


# Firing outranks unknown, which outranks healthy, which outranks disabled: when several signals share a
# fingerprint, an unknown must not resolve an incident that another signal still reports as firing.
_PRECEDENCE = {
    HealthState.DISABLED: 0,
    HealthState.HEALTHY: 1,
    HealthState.UNKNOWN: 2,
    HealthState.WARNING: 3,
    HealthState.CRITICAL: 3,
}


def _classify(signals: Iterable[Signal]) -> dict[str, _Observed]:
    observed: dict[str, _Observed] = {}
    for signal in signals:
        for item in _observe(signal):
            _merge(observed, item)
    return observed


def _merge(observed: dict[str, _Observed], item: _Observed) -> None:
    existing = observed.get(item.fingerprint)
    if existing is None:
        observed[item.fingerprint] = item
        return
    winner = item if _rank(item) > _rank(existing) else existing
    observed[item.fingerprint] = replace(winner, run_id=winner.run_id or item.run_id or existing.run_id)


def _rank(item: _Observed) -> tuple[int, int]:
    return _PRECEDENCE[item.health], 1 if item.severity is AlertSeverity.CRITICAL else 0


def _observe(signal: Signal) -> list[_Observed]:
    scope = Scope(stage=signal.stage, domain=signal.domain, product=signal.product, table=signal.table)
    observed: list[_Observed] = []
    for kind in _kinds_of(signal):
        severity = _severity_of(kind, signal.state)
        if signal.state in (HealthState.WARNING, HealthState.CRITICAL) and severity is None:
            # A state this kind does not alert on (a canceled run, a freshness warning) says nothing either way.
            continue
        component = signal.component if kind is AlertKind.STAGE_UNREADABLE else None
        observed.append(
            _Observed(
                kind=kind,
                scope=scope,
                component=component,
                health=signal.state,
                severity=severity,
                reason=signal.reason if signal.reason in _REASONS else _DEFAULT_REASON[kind],
                run_id=signal.run,
                fingerprint=fingerprint(scope, kind, component),
            )
        )
    return observed


def _kinds_of(signal: Signal) -> tuple[AlertKind, ...]:
    if signal.kind is SignalKind.EXECUTION:
        return (AlertKind.RUN_FAILED,)
    if signal.kind is SignalKind.EXPECTED_RUN:
        if signal.state is HealthState.WARNING:
            return (AlertKind.SCHEDULE_MISMATCH,)
        if signal.state is HealthState.CRITICAL:
            return (AlertKind.MISSED_RUN,)
        # Healthy, disabled and unknown apply to both incident kinds this signal can have opened.
        return (AlertKind.MISSED_RUN, AlertKind.SCHEDULE_MISMATCH)
    if signal.kind is SignalKind.FRESHNESS:
        return (AlertKind.STALE,)
    if signal.kind is SignalKind.QUALITY:
        return (AlertKind.QUALITY,)
    if signal.kind is SignalKind.SERVICE and signal.component == DAGSTER_COMPONENT:
        return (AlertKind.STAGE_UNREADABLE,)
    return ()


def _severity_of(kind: AlertKind, state: HealthState) -> AlertSeverity | None:
    if kind is AlertKind.QUALITY:
        return {HealthState.WARNING: AlertSeverity.WARNING, HealthState.CRITICAL: AlertSeverity.CRITICAL}.get(state)
    if kind is AlertKind.SCHEDULE_MISMATCH:
        return AlertSeverity.WARNING if state is HealthState.WARNING else None
    if kind is AlertKind.STAGE_UNREADABLE:
        # Becomes critical only through the consecutive-unknown counter, never from a single signal.
        return AlertSeverity.CRITICAL if state is HealthState.UNKNOWN else None
    return AlertSeverity.CRITICAL if state is HealthState.CRITICAL else None


def _is_firing(incident: Incident) -> bool:
    return incident.state is IncidentState.FIRING


def _open(item: _Observed, now: datetime) -> Incident:
    assert item.severity is not None
    return Incident(
        id=f"{item.fingerprint}-{int(now.timestamp())}",
        fingerprint=item.fingerprint,
        kind=item.kind,
        severity=item.severity,
        scope=item.scope,
        opened_at=now,
        last_seen_at=now,
        reason=item.reason,
        component=item.component,
        run_ids=_remember((), item.run_id),
    )


def _remember(run_ids: tuple[str, ...], run_id: str | None) -> tuple[str, ...]:
    if run_id is None or run_id in run_ids:
        return run_ids
    return (*run_ids, run_id)[-MAX_RUN_IDS:]


def _notify(
    event: AlertEvent,
    incident: Incident,
    now: datetime,
    run_id: str | None,
    links: Callable[[Incident], Mapping[str, str]] | None,
    *,
    reason: str | None = None,
) -> Notification:
    return Notification(
        id=f"{incident.id}:{event.value}",
        event=event,
        incident_id=incident.id,
        severity=incident.severity,
        kind=incident.kind,
        scope=incident.scope,
        reason=reason or incident.reason,
        occurred_at=now,
        opened_at=incident.opened_at,
        run_id=run_id,
        links=tuple(sorted(links(incident).items())) if links else (),
    )


def _state_to_dict(state: AlertState) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "incidents": [_incident_to_dict(incident) for incident in state.incidents],
        "outbox": [
            {
                "notification": _notification_to_dict(entry.notification),
                "attempts": dict(sorted(entry.attempts.items())),
            }
            for entry in state.outbox
        ],
        "unknown_counts": dict(sorted(state.unknown_counts.items())),
    }


def _scope_to_dict(scope: Scope) -> dict[str, str | None]:
    return {"stage": scope.stage, "domain": scope.domain, "product": scope.product, "table": scope.table}


def _incident_to_dict(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "fingerprint": incident.fingerprint,
        "kind": incident.kind.value,
        "severity": incident.severity.value,
        "scope": _scope_to_dict(incident.scope),
        "opened_at": incident.opened_at.isoformat(),
        "last_seen_at": incident.last_seen_at.isoformat(),
        "reason": incident.reason,
        "state": incident.state.value,
        "component": incident.component,
        "run_ids": list(incident.run_ids),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
    }


def _notification_to_dict(notification: Notification) -> dict[str, Any]:
    return {
        "id": notification.id,
        "event": notification.event.value,
        "incident_id": notification.incident_id,
        "severity": notification.severity.value,
        "kind": notification.kind.value,
        "scope": _scope_to_dict(notification.scope),
        "reason": notification.reason,
        "occurred_at": notification.occurred_at.isoformat(),
        "opened_at": notification.opened_at.isoformat(),
        "run_id": notification.run_id,
        "links": [list(pair) for pair in notification.links],
    }


def _state_from_dict(document: Any) -> AlertState:
    if not isinstance(document, dict):
        raise AlertStateError("alert state must be a JSON object")
    version = document.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        raise AlertStateError(f"unsupported alert state schema_version {version!r}; expected {STATE_SCHEMA_VERSION}")
    try:
        return AlertState(
            incidents=tuple(_incident_from_dict(item) for item in document["incidents"]),
            outbox=tuple(
                OutboxEntry(
                    notification=_notification_from_dict(item["notification"]),
                    attempts={str(receiver): int(count) for receiver, count in item["attempts"].items()},
                )
                for item in document["outbox"]
            ),
            unknown_counts={str(key): int(count) for key, count in document["unknown_counts"].items()},
        )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise AlertStateError(f"alert state is malformed: {error!r}") from error


def _scope_from_dict(item: Mapping[str, Any]) -> Scope:
    return Scope(stage=item["stage"], domain=item["domain"], product=item["product"], table=item["table"])


def _moment(value: str | None) -> datetime | None:
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment


def _incident_from_dict(item: Mapping[str, Any]) -> Incident:
    opened_at, last_seen_at = _moment(item["opened_at"]), _moment(item["last_seen_at"])
    if opened_at is None or last_seen_at is None:
        raise ValueError("incident timestamps are required")
    return Incident(
        id=item["id"],
        fingerprint=item["fingerprint"],
        kind=AlertKind(item["kind"]),
        severity=AlertSeverity(item["severity"]),
        scope=_scope_from_dict(item["scope"]),
        opened_at=opened_at,
        last_seen_at=last_seen_at,
        reason=item["reason"],
        state=IncidentState(item["state"]),
        component=item["component"],
        run_ids=tuple(item["run_ids"]),
        resolved_at=_moment(item["resolved_at"]),
    )


def _notification_from_dict(item: Mapping[str, Any]) -> Notification:
    occurred_at, opened_at = _moment(item["occurred_at"]), _moment(item["opened_at"])
    if occurred_at is None or opened_at is None:
        raise ValueError("notification timestamps are required")
    return Notification(
        id=item["id"],
        event=AlertEvent(item["event"]),
        incident_id=item["incident_id"],
        severity=AlertSeverity(item["severity"]),
        kind=AlertKind(item["kind"]),
        scope=_scope_from_dict(item["scope"]),
        reason=item["reason"],
        occurred_at=occurred_at,
        opened_at=opened_at,
        run_id=item["run_id"],
        links=tuple((str(name), str(url)) for name, url in item["links"]),
    )
