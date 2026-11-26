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
    Delivery,
    DeliveryOutcome,
    DeliveryTarget,
    Incident,
    Notification,
    OutboxEntry,
    TargetStatus,
)
from .health import Scope

STATE_SCHEMA_VERSION = 1
# Evaluator runs (5 minutes apart, about an hour) a notification may wait before it is undeliverable.
MAX_OUTBOX_RUNS = 12
# An incident is notified at most once per event (a notification's id is `<incident>:<event>`, and `notified_severity`
# only rises, so it escalates once at most), and it opens before it escalates: the event alone orders its notifications.
# They can settle out of that order, as an opening that waits out its expiry behind a delivered escalation does.
_NOTIFICATION_ORDER = (AlertEvent.OPENED, AlertEvent.ESCALATED, AlertEvent.RESOLVED)


class AlertStateError(ValueError):
    """The persisted alert state cannot be read; the caller decides whether to keep a copy and start over."""


def record_attempt(state: AlertState, notification_id: str, receiver: str) -> AlertState:
    """Count one delivery attempt of a queued notification to a receiver."""
    return _update(
        state,
        notification_id,
        lambda entry: replace(entry, attempts={**entry.attempts, receiver: entry.attempts.get(receiver, 0) + 1}),
    )


def record_delivery(
    state: AlertState,
    notification_id: str,
    receiver: str,
    *,
    routed: Collection[str],
    rejected: bool = False,
    url: str | None = None,
) -> AlertState:
    """Mark a receiver as having settled a notification; the entry goes once every routed receiver has.

    `rejected` settles it without delivering: the receiver refused it in a way a retry will not change. `url` is where
    the receiver says it landed (a GitHub discussion), kept for the Portal to link.
    """
    refused = frozenset({receiver}) if rejected else frozenset()
    landed = {receiver: url} if url and not rejected else {}
    marked = _update(
        state,
        notification_id,
        lambda entry: replace(
            entry,
            delivered=entry.delivered | {receiver},
            rejected=entry.rejected | refused,
            urls={**entry.urls, **landed},
        ),
    )
    return _settle(
        marked,
        [e for e in marked.outbox if e.notification.id == notification_id and set(routed) <= e.delivered],
    )


def drop_notification(state: AlertState, notification_id: str) -> AlertState:
    """Remove a notification nobody is left to send to: no receiver is routed, or every routed one has settled it."""
    return _settle(state, [entry for entry in state.outbox if entry.notification.id == notification_id])


def expire_outbox(state: AlertState, max_runs: int = MAX_OUTBOX_RUNS) -> tuple[AlertState, list[OutboxEntry]]:
    """Drop entries that have waited `max_runs` evaluations; the caller records them as `undeliverable`."""
    if max_runs < 1:
        raise ValueError("max_runs must be at least 1")
    expired = [entry for entry in state.outbox if entry.runs >= max_runs]
    return _settle(state, expired, expired=True), expired


def notification_order(event: AlertEvent) -> int:
    """Where an incident's notification of `event` falls among its others: a higher one is the later notification."""
    return _NOTIFICATION_ORDER.index(event)


def _settle(state: AlertState, entries: list[OutboxEntry], *, expired: bool = False) -> AlertState:
    """Remove `entries` from the outbox, keeping on each open incident the outcome of its latest settled notification.

    An older notification that settles after a newer one leaves the newer one's outcome in place.
    """
    gone = {entry.notification.id for entry in entries}
    outcomes: dict[str, Delivery] = {}
    for entry in entries:
        outcome = _delivery(entry, expired=expired)
        if not _older(outcome, outcomes.get(entry.notification.incident_id)):
            outcomes[entry.notification.incident_id] = outcome
    return replace(
        state,
        incidents=tuple(
            replace(incident, delivery=outcomes[incident.id])
            if incident.id in outcomes and not _older(outcomes[incident.id], incident.delivery)
            else incident
            for incident in state.incidents
        ),
        outbox=tuple(entry for entry in state.outbox if entry.notification.id not in gone),
    )


def _older(outcome: Delivery, than: Delivery | None) -> bool:
    return than is not None and notification_order(outcome.event) < notification_order(than.event)


def _delivery(entry: OutboxEntry, *, expired: bool) -> Delivery:
    receivers = set(entry.attempts) | entry.delivered
    accepted = entry.delivered - entry.rejected
    if expired or entry.rejected:
        outcome = DeliveryOutcome.UNDELIVERABLE
    elif not accepted:
        # Not expired, refused by nobody and accepted by nobody: it left because no receiver is routed to it, from the
        # start or after those it was being retried to were removed from the routes. Nobody was told.
        outcome = DeliveryOutcome.UNROUTED
    else:
        outcome = DeliveryOutcome.DELIVERED
    return Delivery(
        event=entry.notification.event,
        outcome=outcome,
        receivers=len(receivers),
        delivered=len(accepted),
        attempts=sum(entry.attempts.values()),
        targets=delivery_targets(entry, settled=True, expired=expired),
    )


def delivery_targets(entry: OutboxEntry, *, settled: bool = False, expired: bool = False) -> tuple[DeliveryTarget, ...]:
    """Each receiver the entry was sent to, by name, and where it stands.

    A queued entry's unsettled receivers are still retrying. Once it settles they either expired with it or, when it
    left because they were taken out of the routes, are no longer its receivers and are left out.
    """
    targets = []
    for receiver in sorted(set(entry.attempts) | entry.delivered):
        if receiver in entry.rejected:
            status = TargetStatus.REJECTED
        elif receiver in entry.delivered:
            status = TargetStatus.DELIVERED
        elif expired:
            status = TargetStatus.EXPIRED
        elif settled:
            continue
        else:
            status = TargetStatus.RETRYING
        targets.append(DeliveryTarget(receiver, status, entry.attempts.get(receiver, 0), entry.urls.get(receiver)))
    return tuple(targets)


def _update(state: AlertState, notification_id: str, change: Any) -> AlertState:
    return replace(
        state,
        outbox=tuple(change(entry) if entry.notification.id == notification_id else entry for entry in state.outbox),
    )


def state_to_json(state: AlertState) -> str:
    document = {
        "schema_version": STATE_SCHEMA_VERSION,
        "incidents": [_incident_to_dict(incident) for incident in state.incidents],
        "outbox": [_entry_to_dict(entry) for entry in state.outbox],
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


# `rejected`, `urls`, `delivery` and its `targets` are written only when set, so a state without them serializes exactly
# as the evaluator before them wrote it: pending history is tagged with a digest of that text (`state_digest` in the
# platform API), and a digest that changed across an upgrade would drop history still waiting to be replayed.
def _entry_to_dict(entry: OutboxEntry) -> dict[str, Any]:
    item: dict[str, Any] = {
        "notification": _notification_to_dict(entry.notification),
        "attempts": dict(sorted(entry.attempts.items())),
        "delivered": sorted(entry.delivered),
        "runs": entry.runs,
    }
    if entry.rejected:
        item["rejected"] = sorted(entry.rejected)
    if entry.urls:
        item["urls"] = dict(sorted(entry.urls.items()))
    return item


def _scope_to_dict(scope: Scope) -> dict[str, str | None]:
    return {"stage": scope.stage, "domain": scope.domain, "product": scope.product, "table": scope.table}


def _incident_to_dict(incident: Incident) -> dict[str, Any]:
    item: dict[str, Any] = {
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
    if incident.delivery is not None:
        item["delivery"] = _delivery_to_dict(incident.delivery)
    return item


def _delivery_to_dict(delivery: Delivery) -> dict[str, Any]:
    item: dict[str, Any] = {
        "event": delivery.event.value,
        "outcome": delivery.outcome.value,
        "receivers": delivery.receivers,
        "delivered": delivery.delivered,
        "attempts": delivery.attempts,
    }
    if delivery.targets:
        item["targets"] = [_target_to_dict(target) for target in delivery.targets]
    return item


def _target_to_dict(target: DeliveryTarget) -> dict[str, Any]:
    return {"receiver": target.receiver, "status": target.status.value, "attempts": target.attempts, "url": target.url}


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
        # Absent from a state written before outcomes were kept: unknown, not delivered.
        delivery=None if item.get("delivery") is None else _delivery_from_dict(item["delivery"]),
    )


def _delivery_from_dict(value: Any) -> Delivery:
    item = _dict(value)
    return Delivery(
        event=AlertEvent(_str(item["event"])),
        outcome=DeliveryOutcome(_str(item["outcome"])),
        receivers=_int(item["receivers"]),
        delivered=_int(item["delivered"]),
        attempts=_int(item["attempts"]),
        targets=tuple(_target_from_dict(target) for target in _list(item.get("targets", []))),
    )


def _target_from_dict(value: Any) -> DeliveryTarget:
    item = _dict(value)
    return DeliveryTarget(
        receiver=_str(item["receiver"]),
        status=TargetStatus(_str(item["status"])),
        attempts=_int(item["attempts"]),
        url=_opt_str(item.get("url")),
    )


def _entry_from_dict(value: Any) -> OutboxEntry:
    item = _dict(value)
    return OutboxEntry(
        notification=_notification_from_dict(item["notification"]),
        attempts=_counts(item["attempts"]),
        delivered=frozenset(_str(receiver) for receiver in _list(item["delivered"])),
        rejected=frozenset(_str(receiver) for receiver in _list(item.get("rejected", []))),
        urls={_str(name): _str(url) for name, url in _dict(item.get("urls", {})).items()},
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
