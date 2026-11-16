from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from openlakeforge_core import (
    UNREADABLE_THRESHOLD,
    AlertEvent,
    AlertKind,
    AlertSeverity,
    AlertState,
    AlertStateError,
    HealthState,
    IncidentState,
    QualityObservation,
    QualityOutcome,
    RunObservation,
    RunStatus,
    ScheduleObservation,
    ScheduleState,
    Scope,
    Signal,
    SignalKind,
    drop_notification,
    evaluate,
    evaluate_execution,
    evaluate_expected_run,
    evaluate_quality,
    fingerprint,
    record_attempt,
    service_signal,
)

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=1)
GRACE = timedelta(minutes=10)
DEV = Scope(stage="dev", domain="sales", product="orders", table="silver.orders")
PROD = Scope(stage="prod", domain="sales", product="orders", table="silver.orders")


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _signal(
    state: HealthState,
    kind: SignalKind = SignalKind.EXECUTION,
    scope: Scope = DEV,
    reason: str = "latest run failed",
    run: str | None = "run-1",
    component: str | None = None,
) -> Signal:
    return Signal(
        kind=kind,
        state=state,
        stage=scope.stage,
        reason=reason,
        domain=scope.domain,
        product=scope.product,
        table=scope.table,
        run=run,
        component=component,
    )


def _failed(scope: Scope = DEV, run: str = "run-1") -> Signal:
    return _signal(HealthState.CRITICAL, scope=scope, run=run)


def _healthy(scope: Scope = DEV) -> Signal:
    return _signal(HealthState.HEALTHY, reason="latest run succeeded", scope=scope)


def _dagster(state: HealthState, stage: str = "dev") -> Signal:
    return service_signal(Scope(stage=stage), "dagster", state, "no observation", now=T0, max_age=MAX_AGE)


def test_new_critical_signal_opens_one_incident_and_notifies_once() -> None:
    state, notifications = evaluate(AlertState(), [_failed()], T0)

    assert [n.event for n in notifications] == [AlertEvent.OPENED]
    (incident,) = state.firing()
    assert incident.kind is AlertKind.RUN_FAILED
    assert incident.severity is AlertSeverity.CRITICAL
    assert incident.run_ids == ("run-1",)
    assert notifications[0].incident_id == incident.id


def test_ten_repeated_failures_notify_once_and_recovery_notifies_once() -> None:
    state = AlertState()
    sent = []
    for minute in range(10):
        state, notifications = evaluate(state, [_failed(run=f"run-{minute}")], _at(5 * minute))
        sent.extend(notifications)
    assert [n.event for n in sent] == [AlertEvent.OPENED]
    assert len(state.firing()) == 1
    assert state.firing()[0].run_ids[0] == "run-0"
    assert state.firing()[0].last_seen_at == _at(45)

    state, notifications = evaluate(state, [_healthy()], _at(50))
    assert [n.event for n in notifications] == [AlertEvent.RESOLVED]
    assert state.firing() == ()
    assert state.incidents[0].state is IncidentState.RESOLVED

    state, notifications = evaluate(state, [_healthy()], _at(55))
    assert notifications == []


def test_a_failure_after_recovery_is_a_new_incident() -> None:
    state, first = evaluate(AlertState(), [_failed()], T0)
    state, _ = evaluate(state, [_healthy()], _at(5))
    state, second = evaluate(state, [_failed()], _at(10))

    assert first[0].incident_id != second[0].incident_id
    assert [n.event for n in second] == [AlertEvent.OPENED]


def test_dev_and_prod_never_merge() -> None:
    state, notifications = evaluate(AlertState(), [_failed(DEV), _failed(PROD)], T0)
    assert len(state.firing()) == 2
    assert len(notifications) == 2
    assert fingerprint(DEV, AlertKind.RUN_FAILED) != fingerprint(PROD, AlertKind.RUN_FAILED)

    state, notifications = evaluate(state, [_healthy(DEV)], _at(5))
    assert [n.scope.stage for n in notifications] == ["dev"]
    assert [i.scope.stage for i in state.firing()] == ["prod"]


def test_fingerprint_separates_scope_kind_and_component() -> None:
    base = fingerprint(DEV, AlertKind.QUALITY)
    assert base != fingerprint(DEV, AlertKind.RUN_FAILED)
    assert base != fingerprint(Scope(stage="dev", domain="sales", product="orders"), AlertKind.QUALITY)
    assert base != fingerprint(DEV, AlertKind.QUALITY, "dagster")


def test_unknown_never_opens_an_incident() -> None:
    state, notifications = evaluate(AlertState(), [_signal(HealthState.UNKNOWN, reason="no observation")], T0)
    assert (state.incidents, notifications) == ((), [])


def test_unknown_never_resolves_an_incident() -> None:
    state, _ = evaluate(AlertState(), [_failed()], T0)
    state, notifications = evaluate(state, [_signal(HealthState.UNKNOWN, reason="no observation")], _at(5))
    assert notifications == []
    assert len(state.firing()) == 1


def test_unknown_does_not_override_a_firing_signal_for_the_same_incident() -> None:
    state, _ = evaluate(AlertState(), [_failed()], T0)
    state, notifications = evaluate(state, [_signal(HealthState.UNKNOWN), _failed(run="run-2")], _at(5))
    assert notifications == []
    assert state.firing()[0].run_ids == ("run-1", "run-2")


def test_stage_unreadable_opens_after_consecutive_unknowns_only() -> None:
    state = AlertState()
    for minute in range(UNREADABLE_THRESHOLD - 1):
        state, notifications = evaluate(state, [_dagster(HealthState.UNKNOWN)], _at(5 * minute))
        assert notifications == []
        assert state.incidents == ()

    state, notifications = evaluate(state, [_dagster(HealthState.UNKNOWN)], _at(15))
    assert [(n.event, n.kind) for n in notifications] == [(AlertEvent.OPENED, AlertKind.STAGE_UNREADABLE)]
    assert state.firing()[0].severity is AlertSeverity.CRITICAL

    state, notifications = evaluate(state, [_dagster(HealthState.UNKNOWN)], _at(20))
    assert notifications == []

    state, notifications = evaluate(state, [_dagster(HealthState.HEALTHY)], _at(25))
    assert [n.event for n in notifications] == [AlertEvent.RESOLVED]
    assert state.unknown_counts == {}


def test_a_healthy_read_resets_the_unreadable_counter() -> None:
    state = AlertState()
    sequence = [HealthState.UNKNOWN, HealthState.UNKNOWN, HealthState.HEALTHY, HealthState.UNKNOWN]
    for minute, health in enumerate(sequence):
        state, notifications = evaluate(state, [_dagster(health)], _at(5 * minute))
        assert notifications == []
    assert state.incidents == ()


def test_unreadable_counters_are_per_stage() -> None:
    state = AlertState()
    for minute in range(UNREADABLE_THRESHOLD):
        signals = [_dagster(HealthState.UNKNOWN, "dev"), _dagster(HealthState.HEALTHY, "prod")]
        state, _ = evaluate(state, signals, _at(minute))
    assert [i.scope.stage for i in state.firing()] == ["dev"]


def test_warning_escalating_to_critical_notifies_once() -> None:
    warning = _signal(HealthState.WARNING, SignalKind.QUALITY, reason="quality checks warned")
    critical = _signal(HealthState.CRITICAL, SignalKind.QUALITY, reason="a blocking quality check failed")

    state, notifications = evaluate(AlertState(), [warning], T0)
    assert [(n.event, n.severity) for n in notifications] == [(AlertEvent.OPENED, AlertSeverity.WARNING)]

    state, notifications = evaluate(state, [critical], _at(5))
    assert [(n.event, n.severity) for n in notifications] == [(AlertEvent.ESCALATED, AlertSeverity.CRITICAL)]

    state, notifications = evaluate(state, [critical], _at(10))
    assert notifications == []

    state, notifications = evaluate(state, [warning], _at(15))
    assert notifications == []
    assert state.firing()[0].severity is AlertSeverity.WARNING


def test_quality_warn_alerts_as_warning_from_the_real_rule() -> None:
    observation = QualityObservation(QualityOutcome.WARN, observed_at=T0, run="run-1")
    signal = evaluate_quality(DEV, observation, now=T0, max_age=MAX_AGE)
    state, notifications = evaluate(AlertState(), [signal], T0)
    assert [(n.kind, n.severity) for n in notifications] == [(AlertKind.QUALITY, AlertSeverity.WARNING)]
    assert state.firing()[0].kind is AlertKind.QUALITY


def test_canceled_runs_do_not_alert_or_resolve() -> None:
    canceled = evaluate_execution(DEV, RunObservation(RunStatus.CANCELED, T0, run="run-1"), now=T0, max_age=MAX_AGE)
    state, notifications = evaluate(AlertState(), [canceled], T0)
    assert (state.incidents, notifications) == ((), [])

    state, _ = evaluate(state, [_failed()], _at(1))
    state, notifications = evaluate(state, [canceled], _at(2))
    assert notifications == []
    assert len(state.firing()) == 1


def test_disable_resolves_silently() -> None:
    expected = _signal(HealthState.CRITICAL, SignalKind.EXPECTED_RUN, reason="an expected run did not start", run=None)
    state, notifications = evaluate(AlertState(), [expected], _at(0))
    assert [n.kind for n in notifications] == [AlertKind.MISSED_RUN]

    disabled = _signal(HealthState.DISABLED, SignalKind.EXPECTED_RUN, reason="schedule is stopped", run=None)
    state, notifications = evaluate(state, [disabled], _at(5))
    assert notifications == []
    assert state.firing() == ()
    assert state.incidents[0].state is IncidentState.RESOLVED


@pytest.mark.parametrize("schedule_state", [ScheduleState.STOPPED, ScheduleState.ABSENT])
def test_stopped_or_absent_schedule_never_opens_missed_run(schedule_state: ScheduleState) -> None:
    observation = ScheduleObservation(schedule_state, T0, last_expected_at=T0 - timedelta(hours=1))
    signal = evaluate_expected_run(DEV, observation, now=T0, max_age=MAX_AGE, grace=GRACE)
    state, notifications = evaluate(AlertState(), [signal], T0)
    assert (state.incidents, notifications) == ((), [])


def test_due_run_that_never_started_opens_missed_run_and_healthy_resolves_it() -> None:
    due = ScheduleObservation(ScheduleState.RUNNING, T0, last_expected_at=T0 - timedelta(hours=1))
    missed = evaluate_expected_run(DEV, due, now=T0, max_age=MAX_AGE, grace=GRACE)
    state, notifications = evaluate(AlertState(), [missed], T0)
    assert [(n.kind, n.severity) for n in notifications] == [(AlertKind.MISSED_RUN, AlertSeverity.CRITICAL)]

    started = ScheduleObservation(
        ScheduleState.RUNNING, _at(5), last_expected_at=T0 - timedelta(hours=1), last_run_started_at=_at(3)
    )
    ok = evaluate_expected_run(DEV, started, now=_at(5), max_age=MAX_AGE, grace=GRACE)
    state, notifications = evaluate(state, [ok], _at(5))
    assert [n.event for n in notifications] == [AlertEvent.RESOLVED]


def test_schedule_mismatch_is_a_warning_and_resolves_on_healthy() -> None:
    mismatch = _signal(HealthState.WARNING, SignalKind.EXPECTED_RUN, reason="schedule differs", run=None)
    state, notifications = evaluate(AlertState(), [mismatch], T0)
    assert [(n.kind, n.severity) for n in notifications] == [(AlertKind.SCHEDULE_MISMATCH, AlertSeverity.WARNING)]

    healthy = _signal(HealthState.HEALTHY, SignalKind.EXPECTED_RUN, reason="the expected run started", run=None)
    state, notifications = evaluate(state, [healthy], _at(5))
    assert [n.event for n in notifications] == [AlertEvent.RESOLVED]


def test_stale_data_alerts_and_other_signal_kinds_are_ignored() -> None:
    stale = _signal(HealthState.CRITICAL, SignalKind.FRESHNESS, reason="data is stale", run=None)
    latest = _signal(HealthState.CRITICAL, SignalKind.LATEST_SUCCESS, run=None)
    service = _signal(HealthState.CRITICAL, SignalKind.SERVICE, component="trino", run=None)
    _, notifications = evaluate(AlertState(), [stale, latest, service], T0)
    assert [n.kind for n in notifications] == [AlertKind.STALE]


def test_payload_carries_only_allowlisted_fields_and_no_run_text() -> None:
    hostile = Signal(
        kind=SignalKind.EXECUTION,
        state=HealthState.CRITICAL,
        stage="dev",
        reason="latest run failed password=hunter2 row=(1, 'alice@example.com')",
        domain="sales",
        product="orders",
        table="silver.orders",
        run="run-1",
        evidence_uri="evidence://dev/run-1?token=abc",
    )
    _, (notification,) = evaluate(
        AlertState(),
        [hostile],
        T0,
        links=lambda incident: {"run": f"https://dagster.example/runs/{incident.run_ids[0]}"},
    )
    payload = notification.payload()

    assert set(payload) == {
        "version",
        "event",
        "incident_id",
        "severity",
        "kind",
        "stage",
        "domain",
        "product",
        "table",
        "run_id",
        "reason",
        "opened_at",
        "occurred_at",
        "links",
    }
    assert payload["reason"] == "latest run failed"
    assert payload["links"] == {"run": "https://dagster.example/runs/run-1"}
    serialized = json.dumps(payload)
    for secret in ("hunter2", "alice@example.com", "token=abc"):
        assert secret not in serialized


def test_resolved_notification_uses_a_fixed_reason() -> None:
    state, _ = evaluate(AlertState(), [_failed()], T0)
    _, (resolved,) = evaluate(state, [_healthy()], _at(5))
    assert resolved.payload()["reason"] == "recovered"
    assert resolved.payload()["event"] == "resolved"


def test_resolved_incidents_are_pruned_after_retention() -> None:
    state, _ = evaluate(AlertState(), [_failed()], T0)
    state, _ = evaluate(state, [_healthy()], _at(5))
    assert len(state.incidents) == 1
    state, _ = evaluate(state, [], _at(5) + timedelta(days=8))
    assert state.incidents == ()


def test_outbox_records_notifications_and_attempts() -> None:
    state, (notification,) = evaluate(AlertState(), [_failed()], T0)
    assert [entry.notification for entry in state.outbox] == [notification]

    state = record_attempt(record_attempt(state, notification.id, "ops"), notification.id, "ops")
    assert state.outbox[0].attempts == {"ops": 2}
    assert drop_notification(state, notification.id).outbox == ()


def test_state_round_trips_through_json() -> None:
    state, (notification,) = evaluate(
        AlertState(),
        [_failed(), _dagster(HealthState.UNKNOWN)],
        T0,
        links=lambda _: {"docs": "https://docs.example/a"},
    )
    state = record_attempt(state, notification.id, "ops")
    state, _ = evaluate(state, [_healthy()], _at(5))

    restored = AlertState.from_json(state.to_json())

    assert restored == state
    assert restored.to_json() == state.to_json()
    assert json.loads(state.to_json())["schema_version"] == 1


def test_unknown_schema_version_is_refused() -> None:
    document = json.loads(AlertState().to_json())
    document["schema_version"] = 2
    with pytest.raises(AlertStateError, match="schema_version"):
        AlertState.from_json(json.dumps(document))


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"schema_version": 1}',
        '{"schema_version": 1, "incidents": [{}], "outbox": [], "unknown_counts": {}}',
    ],
)
def test_corrupt_state_is_refused(text: str) -> None:
    with pytest.raises(AlertStateError):
        AlertState.from_json(text)


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate(AlertState(), [], datetime(2026, 11, 16, 12, 0))
