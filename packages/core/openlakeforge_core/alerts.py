"""Provider-neutral alert incidents: turn health signals into at most one notification per transition.

Consumers pass in the signals they classified and the state they persisted last time; nothing here performs
I/O, so the same previous state, signals and `now` always give the same new state, notifications and events.

Product rules (maintainer decisions for v0.7): incidents notify once when they open and once when they
resolve; there is no acknowledgement and there are no reminders; a warning that escalates to critical
notifies once more; a resolve caused by a stopped schedule is silent. The state holds open incidents only:
a resolved one lives on in the history events and the outbox.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, NamedTuple

from .alert_policy import ConditionOptions, Freshness, ResolvedPolicy
from .health import HealthState, Scope, Signal, SignalKind

PAYLOAD_SCHEMA = "openlakeforge.alert.v1"
FINGERPRINT_VERSION = "1"
# Unknown evaluations in a row before an unreadable Dagster becomes an incident of its own.
UNREADABLE_THRESHOLD = 3
# The SERVICE components that mean "Dagster could not be read". `dagster.workloads` is pod readiness, not
# readability, so it is deliberately absent.
# `dagster.read-api` is reported on every read, healthy or not. `dagster.pipelines` is reported only when the
# pipeline read fails and is simply absent after a good one, so it can raise an incident but never clear one.
# `stage_unreadable` is therefore one incident per stage, fed by both: the stage counts as unreadable while
# either is UNKNOWN, and as readable again on the first evaluation with a non-UNKNOWN `dagster.read-api` and
# no UNKNOWN `dagster.pipelines`.
DAGSTER_READ_API = "dagster.read-api"
DAGSTER_PIPELINES = "dagster.pipelines"
UNREADABLE_COMPONENTS = frozenset({DAGSTER_READ_API, DAGSTER_PIPELINES})
# The SERVICE component the evaluator reports for a stage's alert policy: WARNING while it cannot be used, HEALTHY once
# it reads cleanly.
ALERT_POLICY = "alert-policy"
# Bounds the state document: a pipeline failing every five minutes must not grow its incident forever.
MAX_RUN_IDS = 20


class AlertKind(StrEnum):
    RUN_FAILED = "run_failed"
    MISSED_RUN = "missed_run"
    SCHEDULE_MISMATCH = "schedule_mismatch"
    STALE_OUTPUT = "stale_output"
    QUALITY = "quality"
    STAGE_UNREADABLE = "stage_unreadable"
    POLICY_UNREADABLE = "policy_unreadable"


# Conditions about the platform, not a product: no rule in alerts.yml governs them and they go to the profile's platform
# receivers (ADR 0023, decision 11).
PLATFORM_KINDS = frozenset({AlertKind.STAGE_UNREADABLE, AlertKind.POLICY_UNREADABLE})


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
    # History only: no receiver is told, so a `Notification` never carries either.
    RESOLVED_SILENTLY = "resolved_silently"
    # An open incident's severity moved without a notification: a fall (critical to warning), or a rise back to a
    # severity already notified. ADR 0023, decision 3.
    SEVERITY_CHANGED = "severity_changed"


HISTORY_ONLY_EVENTS = frozenset({AlertEvent.RESOLVED_SILENTLY, AlertEvent.SEVERITY_CHANGED})

# The `when` key of `alerts.yml` that turns each product condition on. The platform conditions
# (`stage_unreadable`) are absent: no rule governs them (ADR 0023, decision 11).
POLICY_CONDITION = {
    AlertKind.RUN_FAILED: "run_failed",
    AlertKind.MISSED_RUN: "missed_run",
    AlertKind.STALE_OUTPUT: "freshness",
    AlertKind.QUALITY: "quality",
    AlertKind.SCHEDULE_MISMATCH: "schedule_mismatch",
}

_SEVERITY_RANK = {AlertSeverity.WARNING: 0, AlertSeverity.CRITICAL: 1}

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
        "alert policy could not be used",
        "recovered",
    }
)
_DEFAULT_REASON = {
    AlertKind.RUN_FAILED: "latest run failed",
    AlertKind.MISSED_RUN: "an expected run did not start",
    AlertKind.SCHEDULE_MISMATCH: "schedule does not match its descriptor",
    AlertKind.STALE_OUTPUT: "data is stale",
    AlertKind.QUALITY: "a blocking quality check failed",
    AlertKind.STAGE_UNREADABLE: "stage could not be read",
    AlertKind.POLICY_UNREADABLE: "alert policy could not be used",
}
_RECOVERED = "recovered"
_WITHIN_GRACE = "the expected run is within its grace period"
# `health.py` calls a retry in progress, and a due run still inside its grace period, healthy. Neither is
# evidence that the cause is gone, so these kinds resolve on their own reason only; anything else holds.
_RECOVERY_REASONS = {
    AlertKind.RUN_FAILED: frozenset({"latest run succeeded"}),
    AlertKind.MISSED_RUN: frozenset({"the expected run started"}),
}
# A stopped schedule is the only "disabled" that closes an incident without telling anyone.
_SILENT_ON_DISABLED = frozenset({AlertKind.MISSED_RUN, AlertKind.SCHEDULE_MISMATCH})
# These already say why the output is stale, so a second notification would be the duplicate #23 forbids.
_STALE_CAUSES = frozenset({AlertKind.RUN_FAILED, AlertKind.MISSED_RUN})


@dataclass(frozen=True)
class Incident:
    id: str
    fingerprint: str
    kind: AlertKind
    severity: AlertSeverity
    # The highest severity a receiver was told about; escalation compares against it, not against `severity`,
    # so a warning/critical flap cannot notify twice.
    notified_severity: AlertSeverity
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
    """A change worth telling someone about. Every payload field is on the ADR 0023 allowlist."""

    id: str
    event: AlertEvent
    incident_id: str
    severity: AlertSeverity
    kind: AlertKind
    scope: Scope
    reason: str
    event_at: datetime
    opened_at: datetime
    run_id: str | None = None
    links: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.event in HISTORY_ONLY_EVENTS:
            raise ValueError(f"{self.event.value} is recorded in history, never notified")

    def payload(self) -> dict[str, Any]:
        """The webhook body. Built field by field, absent fields omitted: nothing unlisted can reach a receiver."""
        body: dict[str, Any] = {
            "schema": PAYLOAD_SCHEMA,
            "event": self.event.value,
            "incident_id": self.incident_id,
            "condition": self.kind.value,
            "severity": self.severity.value,
            "stage": self.scope.stage,
            "domain": self.scope.domain,
            "product": self.scope.product,
            "table": self.scope.table,
            "run_id": self.run_id,
            "opened_at": self.opened_at.isoformat(),
            "event_at": self.event_at.isoformat(),
            "summary": self.reason,
            "links": dict(self.links),
        }
        return {key: value for key, value in body.items() if value is not None and value != {}}


@dataclass(frozen=True)
class HistoryEvent:
    """One transition for the caller to record, whether or not it produced a notification."""

    event: AlertEvent
    incident: Incident
    event_at: datetime
    run_id: str | None = None


@dataclass(frozen=True)
class OutboxEntry:
    notification: Notification
    # Delivery attempts made per receiver name; the sender owns the retry policy.
    attempts: Mapping[str, int] = field(default_factory=dict)
    # Receivers that have accepted it; the entry is dropped once every routed receiver is here.
    delivered: frozenset[str] = frozenset()
    # Evaluations this entry has waited through, so an undeliverable one can expire.
    runs: int = 0


@dataclass(frozen=True)
class AlertState:
    """Open incidents plus what is still owed to receivers."""

    incidents: tuple[Incident, ...] = ()
    outbox: tuple[OutboxEntry, ...] = ()
    # Consecutive unknown evaluations per "<stage>:<component>", capped at the threshold.
    unknown_counts: Mapping[str, int] = field(default_factory=dict)
    # Incidents opened so far per fingerprint. Never pruned: an id must not repeat, and it is one int each.
    sequences: Mapping[str, int] = field(default_factory=dict)

    def firing(self) -> tuple[Incident, ...]:
        return tuple(incident for incident in self.incidents if incident.state is IncidentState.FIRING)


class Evaluation(NamedTuple):
    state: AlertState
    # To deliver: opened, escalated and resolved.
    notifications: list[Notification]
    # To record: every transition, including `resolved_silently` and `severity_changed`.
    events: list[HistoryEvent]


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
    policy: Mapping[str, ResolvedPolicy],
    links: Callable[[Incident], Mapping[str, str]] | None = None,
    unreadable_threshold: int = UNREADABLE_THRESHOLD,
    active_stages: Collection[str] | None = None,
    active_scopes: Collection[Scope] | None = None,
) -> Evaluation:
    """Advance the incidents by one evaluation. A fingerprint with no signal this time is left as it was.

    `active_stages` and `active_scopes` (stage, domain and product; `table` is ignored) name what still exists.
    An open incident outside them resolves silently and their signals are ignored, so a deleted product or a
    disabled stage cannot stay firing forever. Stage-wide incidents are checked against the stages only.

    `policy` is each stage's resolved alert policy (ADR 0023, decisions 2, 8 and 9). A product condition alerts only
    where a rule enables it for the signal's job, and takes its severity and thresholds from that rule's options. An
    open incident whose condition is no longer enabled resolves silently. A stage absent from `policy` has no policy
    that could be read: nothing new opens there and its open product incidents are held, as for an unreadable stage.
    """
    if now.tzinfo is None:
        raise ValueError("alert timestamps must be timezone-aware")
    now = now.astimezone(UTC)
    observed = {
        key: item
        for key, item in _classify(signals, policy).items()
        if _is_active(item.scope, active_stages, active_scopes)
    }
    incidents = {incident.fingerprint: incident for incident in previous.incidents}
    sequences = dict(previous.sequences)
    notifications: list[Notification] = []
    events: list[HistoryEvent] = []

    def record(
        event: AlertEvent, incident: Incident, run_id: str | None = None, *, notify: bool = True, reason: str = ""
    ) -> None:
        events.append(HistoryEvent(event, incident, now, run_id))
        if notify:
            notifications.append(_notify(event, incident, now, run_id, links, reason or incident.reason))

    for key, incident in list(incidents.items()):
        if not _is_active(incident.scope, active_stages, active_scopes) or _undeclared(incident, policy):
            del incidents[key]
            record(AlertEvent.RESOLVED_SILENTLY, _close(incident, now), notify=False)

    # A stage that sent no unknown Dagster read this evaluation drops out, so the count is of consecutive
    # evaluations: any other signal, or no signal at all, restarts it. The caller sends one for each stage.
    unknown_counts = {
        _unreadable_key(item): min(previous.unknown_counts.get(_unreadable_key(item), 0) + 1, unreadable_threshold)
        for item in observed.values()
        if item.kind is AlertKind.STAGE_UNREADABLE and item.health is HealthState.UNKNOWN
    }

    # Causes first, so a stale output can see whether one of them is open.
    for key, item in sorted(observed.items(), key=lambda pair: pair[1].kind is AlertKind.STALE_OUTPUT):
        current = incidents.get(key)
        if current and item.grace_due and item.grace_due <= current.opened_at:
            # The incident opened for this very occurrence, which now reads as inside its grace: a rule raised the
            # grace. A later occurrence came due after the incident opened, and does not clear an older miss.
            item = replace(item, health=HealthState.HEALTHY)
        if (
            item.kind is AlertKind.STAGE_UNREADABLE
            and unknown_counts.get(_unreadable_key(item), 0) >= unreadable_threshold
        ):
            item = replace(item, health=HealthState.CRITICAL, reason=_DEFAULT_REASON[item.kind])
        if item.health in (HealthState.WARNING, HealthState.CRITICAL) and item.severity is not None:
            if current is None:
                if item.kind is AlertKind.STALE_OUTPUT and _has_cause(incidents.values(), item.scope):
                    continue
                sequences[key] = sequences.get(key, 0) + 1
                current = _open(item, f"{key}-{int(now.timestamp())}-{sequences[key]}", now)
                incidents[key] = current
                record(AlertEvent.OPENED, current, item.run_id)
                continue
            updated = replace(
                current, last_seen_at=now, run_ids=_remember(current.run_ids, item.run_id), severity=item.severity
            )
            if _SEVERITY_RANK[item.severity] > _SEVERITY_RANK[current.notified_severity]:
                updated = replace(updated, notified_severity=item.severity, reason=item.reason)
                record(AlertEvent.ESCALATED, updated, item.run_id)
            elif item.severity is not current.severity:
                record(AlertEvent.SEVERITY_CHANGED, updated, item.run_id, notify=False)
            incidents[key] = updated
        elif current is not None and item.health is HealthState.HEALTHY:
            resolved = _close(current, now, item.run_id)
            del incidents[key]
            record(AlertEvent.RESOLVED, resolved, item.run_id, reason=_RECOVERED)
        elif current is not None and item.health is HealthState.DISABLED and item.kind in _SILENT_ON_DISABLED:
            del incidents[key]
            record(AlertEvent.RESOLVED_SILENTLY, _close(current, now), notify=False)

    state = AlertState(
        incidents=tuple(sorted(incidents.values(), key=lambda incident: (incident.opened_at, incident.id))),
        outbox=(
            *(replace(entry, runs=entry.runs + 1) for entry in previous.outbox),
            *(OutboxEntry(notification) for notification in notifications),
        ),
        unknown_counts=dict(sorted(unknown_counts.items())),
        sequences=dict(sorted(sequences.items())),
    )
    return Evaluation(state, notifications, events)


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
    # Set for a missed-run observation that is only inside its grace period: when its occurrence came due.
    grace_due: datetime | None = None


# Firing outranks unknown, which outranks healthy, which outranks disabled: when several signals share a
# fingerprint, an unknown must not resolve an incident that another signal still reports as firing.
_PRECEDENCE = {
    HealthState.DISABLED: 0,
    HealthState.HEALTHY: 1,
    HealthState.UNKNOWN: 2,
    HealthState.WARNING: 3,
    HealthState.CRITICAL: 3,
}


def _classify(signals: Iterable[Signal], policy: Mapping[str, ResolvedPolicy]) -> dict[str, _Observed]:
    observed: dict[str, _Observed] = {}
    for signal in signals:
        for item in _observe(signal, policy):
            existing = observed.get(item.fingerprint)
            winner = item if existing is None or _rank(item) > _rank(existing) else existing
            run_id = winner.run_id or item.run_id or (existing.run_id if existing else None)
            observed[item.fingerprint] = replace(winner, run_id=run_id)
    return observed


def _rank(item: _Observed) -> tuple[int, int]:
    if item.kind is AlertKind.STAGE_UNREADABLE and item.health is HealthState.UNKNOWN:
        # Either read being unknown keeps the stage unreadable, even beside a degraded answer from the other.
        return 4, 0
    return _PRECEDENCE[item.health], 1 if item.severity is AlertSeverity.CRITICAL else 0


def _declared(policy: Mapping[str, ResolvedPolicy], scope: Scope, kind: AlertKind) -> ConditionOptions | None:
    """The options of the rule that enables `kind` for the scope's job; None when no rule does or the stage has none."""
    resolved = policy.get(scope.stage)
    if resolved is None:
        return None
    conditions = resolved.for_job(scope.product) if scope.product is not None else resolved.unmatched
    found = conditions.get(POLICY_CONDITION[kind])
    return found.options if found else None


def _undeclared(incident: Incident, policy: Mapping[str, ResolvedPolicy]) -> bool:
    """Whether a rule change has switched the incident's condition off. A stage with no policy holds its incidents."""
    return (
        incident.kind in POLICY_CONDITION
        and incident.scope.stage in policy
        and _declared(policy, incident.scope, incident.kind) is None
    )


def _observe(signal: Signal, policy: Mapping[str, ResolvedPolicy]) -> list[_Observed]:
    scope = Scope(stage=signal.stage, domain=signal.domain, product=signal.product, table=signal.table)
    observed: list[_Observed] = []
    for kind in _kinds_of(signal):
        options = _declared(policy, scope, kind) if kind in POLICY_CONDITION else None
        if kind in POLICY_CONDITION and options is None:
            continue
        severity = _severity_of(kind, signal.state, options)
        health = signal.state
        if health in (HealthState.WARNING, HealthState.CRITICAL) and severity is None:
            if kind is not AlertKind.STAGE_UNREADABLE:
                # A state this kind does not alert on (a canceled run, a freshness warning) says nothing either way.
                continue
            # A Dagster that answered, even degraded (no code location loaded, a repository-loading error), is
            # readable: for this condition the answer is a recovery observation, which restarts the counter and
            # resolves an open incident. An UNKNOWN pipeline read beside it still wins (see `_rank`).
            health = HealthState.HEALTHY
        elif health is HealthState.HEALTHY and signal.reason not in _RECOVERY_REASONS.get(kind, {signal.reason}):
            health = HealthState.UNKNOWN
        if (
            kind is AlertKind.STAGE_UNREADABLE
            and signal.component == DAGSTER_PIPELINES
            and health is not HealthState.UNKNOWN
        ):
            # Only a failed pipeline read means anything; it is never evidence that the stage is readable.
            continue
        component = None
        observed.append(
            _Observed(
                kind=kind,
                scope=scope,
                component=component,
                health=health,
                severity=severity,
                reason=signal.reason if signal.reason in _REASONS else _DEFAULT_REASON[kind],
                run_id=signal.run,
                fingerprint=fingerprint(scope, kind, component),
                grace_due=signal.due if kind is AlertKind.MISSED_RUN and signal.reason == _WITHIN_GRACE else None,
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
        return (AlertKind.STALE_OUTPUT,)
    if signal.kind is SignalKind.QUALITY:
        return (AlertKind.QUALITY,)
    if signal.kind is SignalKind.SERVICE and signal.component in UNREADABLE_COMPONENTS:
        return (AlertKind.STAGE_UNREADABLE,)
    if signal.kind is SignalKind.SERVICE and signal.component == ALERT_POLICY:
        return (AlertKind.POLICY_UNREADABLE,)
    return ()


def _severity_of(kind: AlertKind, state: HealthState, options: ConditionOptions | None) -> AlertSeverity | None:
    if kind is AlertKind.POLICY_UNREADABLE:
        return AlertSeverity.WARNING if state is HealthState.WARNING else None
    if kind is AlertKind.STAGE_UNREADABLE:
        # Becomes critical only through the consecutive-unknown counter, never from a single signal.
        return AlertSeverity.CRITICAL if state is HealthState.UNKNOWN else None
    if kind is AlertKind.QUALITY:
        return {HealthState.WARNING: AlertSeverity.WARNING, HealthState.CRITICAL: AlertSeverity.CRITICAL}.get(state)
    if isinstance(options, Freshness) and (options.warn_after is not None or options.critical_after is not None):
        # Age bounds carry their own severity: past warn_after is a warning, past critical_after is critical.
        return {HealthState.WARNING: AlertSeverity.WARNING, HealthState.CRITICAL: AlertSeverity.CRITICAL}.get(state)
    firing = HealthState.WARNING if kind is AlertKind.SCHEDULE_MISMATCH else HealthState.CRITICAL
    severity = getattr(options, "severity", None)
    return AlertSeverity(severity) if state is firing and severity else None


def _unreadable_key(item: _Observed) -> str:
    return item.scope.stage


def _is_active(scope: Scope, stages: Collection[str] | None, scopes: Collection[Scope] | None) -> bool:
    if stages is not None and scope.stage not in stages:
        return False
    if scopes is not None and scope.product is not None:
        return _scope_key(scope) in {_scope_key(candidate) for candidate in scopes}
    return True


def _scope_key(scope: Scope) -> tuple[str, str | None, str | None]:
    return scope.stage, scope.domain, scope.product


def _has_cause(incidents: Iterable[Incident], scope: Scope) -> bool:
    return any(
        incident.kind in _STALE_CAUSES
        and (incident.scope.stage, incident.scope.domain, incident.scope.product)
        == (scope.stage, scope.domain, scope.product)
        for incident in incidents
    )


def _open(item: _Observed, incident_id: str, now: datetime) -> Incident:
    assert item.severity is not None
    return Incident(
        id=incident_id,
        fingerprint=item.fingerprint,
        kind=item.kind,
        severity=item.severity,
        notified_severity=item.severity,
        scope=item.scope,
        opened_at=now,
        last_seen_at=now,
        reason=item.reason,
        component=item.component,
        run_ids=_remember((), item.run_id),
    )


def _close(incident: Incident, now: datetime, run_id: str | None = None) -> Incident:
    return replace(
        incident,
        state=IncidentState.RESOLVED,
        resolved_at=now,
        last_seen_at=now,
        run_ids=_remember(incident.run_ids, run_id),
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
    reason: str,
) -> Notification:
    return Notification(
        id=f"{incident.id}:{event.value}",
        event=event,
        incident_id=incident.id,
        severity=incident.severity,
        kind=incident.kind,
        scope=incident.scope,
        reason=reason,
        event_at=now,
        opened_at=incident.opened_at,
        run_id=run_id,
        links=tuple(sorted(links(incident).items())) if links else (),
    )
