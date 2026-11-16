"""Persistence helpers for alert state: the outbox of undelivered notifications and the JSON document.

Kept apart from `alerts.py` so the transition model stays small. Nothing here performs I/O either: the caller
reads and writes the document and sends the notifications.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .alerts import (
    AlertEvent,
    AlertKind,
    AlertSeverity,
    AlertState,
    Incident,
    Notification,
    OutboxEntry,
)
from .health import Scope

STATE_SCHEMA_VERSION = 1
# Evaluator runs (5 minutes apart, about an hour) a notification may wait before it is undeliverable.
MAX_OUTBOX_RUNS = 12


class AlertStateError(ValueError):
    """The persisted alert state cannot be read; the caller decides whether to keep a copy and start over."""


def record_attempt(state: AlertState, notification_id: str, receiver: str) -> AlertState:
    """Count one delivery attempt of a queued notification to a receiver."""
    return _update(
        state,
        notification_id,
        lambda entry: replace(entry, attempts={**entry.attempts, receiver: entry.attempts.get(receiver, 0) + 1}),
    )


def record_delivery(state: AlertState, notification_id: str, receiver: str, *, routed: Collection[str]) -> AlertState:
    """Mark a receiver as having accepted a notification; the entry goes once every routed receiver has it."""
    marked = _update(
        state, notification_id, lambda entry: replace(entry, delivered=entry.delivered | frozenset({receiver}))
    )
    return replace(
        marked,
        outbox=tuple(
            entry
            for entry in marked.outbox
            if entry.notification.id != notification_id or not set(routed) <= entry.delivered
        ),
    )


def drop_notification(state: AlertState, notification_id: str) -> AlertState:
    """Remove a notification outright, for a sender that has decided it can never be delivered."""
    return replace(state, outbox=tuple(entry for entry in state.outbox if entry.notification.id != notification_id))


def expire_outbox(state: AlertState, max_runs: int = MAX_OUTBOX_RUNS) -> tuple[AlertState, list[OutboxEntry]]:
    """Drop entries that have waited `max_runs` evaluations; the caller records them as `undeliverable`."""
    if max_runs < 1:
        raise ValueError("max_runs must be at least 1")
    expired = [entry for entry in state.outbox if entry.runs >= max_runs]
    kept = tuple(entry for entry in state.outbox if entry.runs < max_runs)
    return replace(state, outbox=kept), expired


def _update(state: AlertState, notification_id: str, change: Any) -> AlertState:
    return replace(
        state,
        outbox=tuple(change(entry) if entry.notification.id == notification_id else entry for entry in state.outbox),
    )


def state_to_json(state: AlertState) -> str:
    document = {
        "schema_version": STATE_SCHEMA_VERSION,
        "incidents": [_incident_to_dict(incident) for incident in state.incidents],
        "outbox": [
            {
                "notification": _notification_to_dict(entry.notification),
                "attempts": dict(sorted(entry.attempts.items())),
                "delivered": sorted(entry.delivered),
                "runs": entry.runs,
            }
            for entry in state.outbox
        ],
        "unknown_counts": dict(sorted(state.unknown_counts.items())),
        "sequences": dict(sorted(state.sequences.items())),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def state_from_json(text: str) -> AlertState:
    try:
        document = json.loads(text)
    except ValueError as error:
        raise AlertStateError(f"alert state is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise AlertStateError("alert state must be a JSON object")
    version = document.get("schema_version")
    if type(version) is not int or version != STATE_SCHEMA_VERSION:
        raise AlertStateError(f"unsupported alert state schema_version {version!r}; expected {STATE_SCHEMA_VERSION}")
    try:
        return AlertState(
            incidents=tuple(_incident_from_dict(item) for item in _list(document["incidents"])),
            outbox=tuple(_entry_from_dict(item) for item in _list(document["outbox"])),
            unknown_counts=_counts(document["unknown_counts"]),
            sequences=_counts(document["sequences"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AlertStateError(f"alert state is malformed: {error!r}") from error


def _scope_to_dict(scope: Scope) -> dict[str, str | None]:
    return {"stage": scope.stage, "domain": scope.domain, "product": scope.product, "table": scope.table}


def _incident_to_dict(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "fingerprint": incident.fingerprint,
        "kind": incident.kind.value,
        "severity": incident.severity.value,
        "notified_severity": incident.notified_severity.value,
        "scope": _scope_to_dict(incident.scope),
        "opened_at": incident.opened_at.isoformat(),
        "last_seen_at": incident.last_seen_at.isoformat(),
        "reason": incident.reason,
        "component": incident.component,
        "run_ids": list(incident.run_ids),
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
        "event_at": notification.event_at.isoformat(),
        "opened_at": notification.opened_at.isoformat(),
        "run_id": notification.run_id,
        "links": [list(pair) for pair in notification.links],
    }


# The readers below refuse anything but the exact types the writer produces: `True` is not a version,
# a string is not a list of run ids, and a negative counter is not a count.
def _int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"expected a non-negative integer, got {value!r}")
    return value


def _str(value: Any) -> str:
    if type(value) is not str:
        raise ValueError(f"expected a string, got {value!r}")
    return value


def _opt_str(value: Any) -> str | None:
    return None if value is None else _str(value)


def _list(value: Any) -> list[Any]:
    if type(value) is not list:
        raise ValueError(f"expected a list, got {value!r}")
    return value


def _dict(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"expected an object, got {value!r}")
    return value


def _counts(value: Any) -> dict[str, int]:
    return {_str(key): _int(count) for key, count in _dict(value).items()}


def _moment(value: Any) -> datetime:
    moment = datetime.fromisoformat(_str(value))
    if moment.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC")
    return moment


def _scope_from_dict(value: Any) -> Scope:
    item = _dict(value)
    return Scope(
        stage=_str(item["stage"]),
        domain=_opt_str(item["domain"]),
        product=_opt_str(item["product"]),
        table=_opt_str(item["table"]),
    )


def _incident_from_dict(value: Any) -> Incident:
    item = _dict(value)
    return Incident(
        id=_str(item["id"]),
        fingerprint=_str(item["fingerprint"]),
        kind=AlertKind(_str(item["kind"])),
        severity=AlertSeverity(_str(item["severity"])),
        notified_severity=AlertSeverity(_str(item["notified_severity"])),
        scope=_scope_from_dict(item["scope"]),
        opened_at=_moment(item["opened_at"]),
        last_seen_at=_moment(item["last_seen_at"]),
        reason=_str(item["reason"]),
        component=_opt_str(item["component"]),
        run_ids=tuple(_str(run) for run in _list(item["run_ids"])),
    )


def _entry_from_dict(value: Any) -> OutboxEntry:
    item = _dict(value)
    return OutboxEntry(
        notification=_notification_from_dict(item["notification"]),
        attempts=_counts(item["attempts"]),
        delivered=frozenset(_str(receiver) for receiver in _list(item["delivered"])),
        runs=_int(item["runs"]),
    )


def _notification_from_dict(value: Any) -> Notification:
    item = _dict(value)
    links = []
    for pair in _list(item["links"]):
        name, url = _list(pair)
        links.append((_str(name), _str(url)))
    return Notification(
        id=_str(item["id"]),
        event=AlertEvent(_str(item["event"])),
        incident_id=_str(item["incident_id"]),
        severity=AlertSeverity(_str(item["severity"])),
        kind=AlertKind(_str(item["kind"])),
        scope=_scope_from_dict(item["scope"]),
        reason=_str(item["reason"]),
        event_at=_moment(item["event_at"]),
        opened_at=_moment(item["opened_at"]),
        run_id=_opt_str(item["run_id"]),
        links=tuple(links),
    )
