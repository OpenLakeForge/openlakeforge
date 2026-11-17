from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from _alerts_support import evaluate
from openlakeforge_core import (
    ALERT_POLICY,
    DAGSTER_PIPELINES,
    DAGSTER_READ_API,
    UNREADABLE_THRESHOLD,
    AlertEvent,
    AlertKind,
    AlertSeverity,
    AlertState,
    HealthState,
    QualityObservation,
    QualityOutcome,
    RunObservation,
    RunStatus,
    ScheduleObservation,
    ScheduleState,
    Scope,
    Signal,
    SignalKind,
    evaluate_execution,
    evaluate_expected_run,
    evaluate_quality,
    fingerprint,
    service_signal,
)

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=1)
GRACE = timedelta(minutes=10)
DEV = Scope(stage="dev", domain="sales", product="orders", table="silver.orders")
PROD = Scope(stage="prod", domain="sales", product="orders", table="silver.orders")
RUN_FAILED, SUCCEEDED, IN_PROGRESS = "latest run failed", "latest run succeeded", "latest run in progress"
NOT_STARTED, STARTED = "an expected run did not start", "the expected run started"


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _signal(
    state: HealthState,
    kind: SignalKind = SignalKind.EXECUTION,
    scope: Scope = DEV,
    reason: str = RUN_FAILED,
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


def _healthy(scope: Scope = DEV, run: str | None = "run-2") -> Signal:
    return _signal(HealthState.HEALTHY, reason=SUCCEEDED, scope=scope, run=run)


def _running() -> Signal:
    return _signal(HealthState.HEALTHY, reason=IN_PROGRESS, run="run-2")


def _missed(state: HealthState = HealthState.CRITICAL, reason: str = NOT_STARTED) -> Signal:
    return _signal(state, SignalKind.EXPECTED_RUN, reason=reason, run=None)


def _quality(state: HealthState) -> Signal:
    reason = "quality checks warned" if state is HealthState.WARNING else "a blocking quality check failed"
    return _signal(state, SignalKind.QUALITY, reason=reason)


def _stale() -> Signal:
    return _signal(HealthState.CRITICAL, SignalKind.FRESHNESS, reason="data is stale", run=None)


def _dagster(state: HealthState, stage: str = "dev", component: str = DAGSTER_READ_API) -> Signal:
    return service_signal(Scope(stage=stage), component, state, "no observation", now=T0, max_age=MAX_AGE)


def _run(state: AlertState, signals: list[Signal], minutes: int, **options):
    """One evaluation, returning what most tests look at: the new state and the notifications."""
    result = evaluate(state, signals, _at(minutes), **options)
    return result.state, result.notifications


def _replay(sequence: list[list[Signal]], **options):
    state, sent = AlertState(), []
    for minutes, signals in enumerate(sequence):
        state, notifications = _run(state, signals, 5 * minutes, **options)
        sent.extend(notifications)
    return state, sent


def _events(sent) -> list[AlertEvent]:
    return [n.event for n in sent]


def test_new_critical_signal_opens_one_incident_and_notifies_once() -> None:
    state, notifications = _run(AlertState(), [_failed()], 0)

    assert _events(notifications) == [AlertEvent.OPENED]
    (incident,) = state.firing()
    assert (incident.kind, incident.severity, incident.notified_severity) == (
        AlertKind.RUN_FAILED,
        AlertSeverity.CRITICAL,
        AlertSeverity.CRITICAL,
    )
    assert incident.run_ids == ("run-1",)
    assert notifications[0].incident_id == incident.id


def test_ten_repeated_failures_notify_once_and_recovery_notifies_once() -> None:
    state, sent = _replay([[_failed(run=f"run-{n}")] for n in range(10)])
    assert _events(sent) == [AlertEvent.OPENED]
    assert state.firing()[0].run_ids[0] == "run-0"
    assert state.firing()[0].last_seen_at == _at(45)

    state, notifications = _run(state, [_healthy()], 50)
    assert _events(notifications) == [AlertEvent.RESOLVED]
    assert state.incidents == ()
    assert _run(state, [_healthy()], 55)[1] == []


def test_a_resolve_carries_the_recovering_run_and_the_fixed_reason() -> None:
    state, _ = _run(AlertState(), [_failed()], 0)
    result = evaluate(state, [_healthy(run="run-9")], _at(5))
    (resolved,) = result.notifications
    assert (resolved.run_id, resolved.reason) == ("run-9", "recovered")
    assert result.events[0].incident.run_ids == ("run-1", "run-9")


def test_a_run_in_progress_neither_resolves_run_failed_nor_reopens_it() -> None:
    state, sent = _replay([[_failed()], [_running()], [_failed(run="run-2")]])
    assert _events(sent) == [AlertEvent.OPENED]
    assert len(state.firing()) == 1

    state, notifications = _run(state, [_healthy()], 15)
    assert _events(notifications) == [AlertEvent.RESOLVED]


@pytest.mark.parametrize("reason", ["the expected run is within its grace period", "no run is due yet"])
def test_missed_run_holds_until_the_expected_run_starts(reason: str) -> None:
    holding = _missed(HealthState.HEALTHY, reason)
    state, sent = _replay([[_missed()], [holding], [_missed()], [holding], [_missed()]])
    assert _events(sent) == [AlertEvent.OPENED]
    assert len(state.firing()) == 1

    state, notifications = _run(state, [_missed(HealthState.HEALTHY, STARTED)], 30)
    assert _events(notifications) == [AlertEvent.RESOLVED]


def test_missed_run_resolves_from_the_real_rules() -> None:
    due = T0 - timedelta(hours=1)
    missed = evaluate_expected_run(
        DEV, ScheduleObservation(ScheduleState.RUNNING, T0, last_expected_at=due), now=T0, max_age=MAX_AGE, grace=GRACE
    )
    state, notifications = _run(AlertState(), [missed], 0)
    assert [n.kind for n in notifications] == [AlertKind.MISSED_RUN]

    started = ScheduleObservation(ScheduleState.RUNNING, _at(5), last_expected_at=due, last_run_started_at=_at(3))
    ok = evaluate_expected_run(DEV, started, now=_at(5), max_age=MAX_AGE, grace=GRACE)
    assert _events(_run(state, [ok], 5)[1]) == [AlertEvent.RESOLVED]


def test_a_failure_after_recovery_is_a_new_incident_with_its_own_id_and_key() -> None:
    state, first = _run(AlertState(), [_failed()], 0)
    state, _ = _run(state, [_healthy()], 0)
    state, second = _run(state, [_failed()], 0)

    assert first[0].incident_id != second[0].incident_id
    assert first[0].id != second[0].id
    assert second[0].incident_id.endswith("-2")
    assert first[0].incident_id.endswith("-1")
    assert _events(second) == [AlertEvent.OPENED]


def test_dev_and_prod_never_merge() -> None:
    state, notifications = _run(AlertState(), [_failed(DEV), _failed(PROD)], 0)
    assert (len(state.firing()), len(notifications)) == (2, 2)
    assert fingerprint(DEV, AlertKind.RUN_FAILED) != fingerprint(PROD, AlertKind.RUN_FAILED)

    state, notifications = _run(state, [_healthy(DEV)], 5)
    assert [n.scope.stage for n in notifications] == ["dev"]
    assert [i.scope.stage for i in state.firing()] == ["prod"]


def test_fingerprint_separates_scope_kind_and_component() -> None:
    base = fingerprint(DEV, AlertKind.QUALITY)
    assert base != fingerprint(DEV, AlertKind.RUN_FAILED)
    assert base != fingerprint(Scope(stage="dev", domain="sales", product="orders"), AlertKind.QUALITY)
    assert base != fingerprint(DEV, AlertKind.QUALITY, DAGSTER_READ_API)


def test_unknown_never_opens_or_resolves_and_never_overrides_a_firing_signal() -> None:
    unknown = _signal(HealthState.UNKNOWN, reason="no observation")
    assert _run(AlertState(), [unknown], 0) == (AlertState(), [])

    state, _ = _run(AlertState(), [_failed()], 0)
    assert _run(state, [unknown], 5)[1] == []
    state, notifications = _run(state, [unknown, _failed(run="run-2")], 10)
    assert notifications == []
    assert state.firing()[0].run_ids == ("run-1", "run-2")


@pytest.mark.parametrize("component", [DAGSTER_READ_API, DAGSTER_PIPELINES])
def test_stage_unreadable_opens_after_three_consecutive_unknown_reads(component: str) -> None:
    unknown = _dagster(HealthState.UNKNOWN, component=component)
    state, sent = _replay([[unknown]] * UNREADABLE_THRESHOLD)
    assert [(n.event, n.kind) for n in sent] == [(AlertEvent.OPENED, AlertKind.STAGE_UNREADABLE)]
    assert state.firing()[0].severity is AlertSeverity.CRITICAL

    state, notifications = _run(state, [unknown], 20)
    assert notifications == []
    state, notifications = _run(state, [_dagster(HealthState.HEALTHY)], 25)
    assert _events(notifications) == [AlertEvent.RESOLVED]
    assert state.unknown_counts == {}


def test_failed_pipeline_reads_followed_by_good_ones_resolve_the_single_incident() -> None:
    failed = [_dagster(HealthState.UNKNOWN, component=DAGSTER_PIPELINES), _dagster(HealthState.HEALTHY)]
    state, sent = _replay([failed] * UNREADABLE_THRESHOLD)
    assert [(n.event, n.kind) for n in sent] == [(AlertEvent.OPENED, AlertKind.STAGE_UNREADABLE)]

    state, notifications = _run(state, failed, 20)
    assert (notifications, len(state.firing())) == ([], 1)
    # The pipelines component is absent after a successful read; the healthy read-api alone must resolve.
    for minutes in (25, 30):
        state, notifications = _run(state, [_dagster(HealthState.HEALTHY)], minutes)
        sent.extend(notifications)
    assert _events(sent) == [AlertEvent.OPENED, AlertEvent.RESOLVED]
    assert (state.incidents, state.unknown_counts) == ((), {})


@pytest.mark.parametrize("answer", [HealthState.WARNING, HealthState.CRITICAL])
def test_a_degraded_but_answered_read_resolves_an_unreadable_stage(answer: HealthState) -> None:
    # WARNING (no code location loaded) and CRITICAL (a repository-loading error) are answers: Dagster is readable.
    state, sent = _replay([[_dagster(HealthState.UNKNOWN)]] * UNREADABLE_THRESHOLD)
    assert _events(sent) == [AlertEvent.OPENED]

    state, notifications = _run(state, [_dagster(answer)], 20)
    assert _events(notifications) == [AlertEvent.RESOLVED]
    assert (state.incidents, state.unknown_counts) == ((), {})


def test_a_degraded_read_api_beside_an_unknown_pipelines_read_keeps_the_stage_unreadable() -> None:
    state, _ = _replay([[_dagster(HealthState.UNKNOWN)]] * UNREADABLE_THRESHOLD)
    mixed = [_dagster(HealthState.WARNING), _dagster(HealthState.UNKNOWN, component=DAGSTER_PIPELINES)]
    state, notifications = _run(state, mixed, 20)
    assert (notifications, len(state.firing())) == ([], 1)


def test_read_api_and_pipelines_unknowns_share_one_incident_and_counter() -> None:
    both = [_dagster(HealthState.UNKNOWN), _dagster(HealthState.UNKNOWN, component=DAGSTER_PIPELINES)]
    pipelines = [_dagster(HealthState.UNKNOWN, component=DAGSTER_PIPELINES)]
    state, sent = _replay([both, [_dagster(HealthState.UNKNOWN)], pipelines])
    assert _events(sent) == [AlertEvent.OPENED]
    assert len(state.firing()) == 1


def test_dagster_workloads_are_not_an_unreadable_stage() -> None:
    state, sent = _replay([[_dagster(HealthState.UNKNOWN, component="dagster.workloads")]] * 5)
    assert (state, sent) == (AlertState(), [])


@pytest.mark.parametrize(
    "between",
    [
        [_dagster(HealthState.HEALTHY)],
        [_dagster(HealthState.WARNING)],
        [_dagster(HealthState.CRITICAL)],
        [],
    ],
    ids=["healthy", "warning", "critical", "no signal"],
)
def test_unknowns_that_are_not_consecutive_never_open_stage_unreadable(between: list[Signal]) -> None:
    unknown = [_dagster(HealthState.UNKNOWN)]
    state, sent = _replay([unknown, unknown, between, unknown, unknown])
    assert (state.incidents, sent) == ((), [])
    assert state.unknown_counts == {"dev": 2}


def test_unreadable_counters_are_per_stage_and_dropped_outside_the_active_stages() -> None:
    signals = [_dagster(HealthState.UNKNOWN, "dev"), _dagster(HealthState.HEALTHY, "prod")]
    state, _ = _replay([signals] * UNREADABLE_THRESHOLD)
    assert [i.scope.stage for i in state.firing()] == ["dev"]

    other = [_dagster(HealthState.UNKNOWN, "qa")]
    state, _ = _run(AlertState(), other, 0, active_stages={"dev"})
    assert state.unknown_counts == {}


def test_warning_and_critical_flapping_escalates_once_per_incident() -> None:
    warning, critical = _quality(HealthState.WARNING), _quality(HealthState.CRITICAL)
    state, sent = _replay([[warning], [critical], [warning], [critical], [warning], [critical]])

    assert [(n.event, n.severity) for n in sent] == [
        (AlertEvent.OPENED, AlertSeverity.WARNING),
        (AlertEvent.ESCALATED, AlertSeverity.CRITICAL),
    ]
    assert len({n.id for n in sent}) == 2
    assert state.firing()[0].notified_severity is AlertSeverity.CRITICAL


def test_escalation_carries_the_escalating_reason_and_resolve_follows() -> None:
    state, _ = _run(AlertState(), [_quality(HealthState.WARNING)], 0)
    state, (escalated,) = _run(state, [_quality(HealthState.CRITICAL)], 5)
    assert escalated.reason == "a blocking quality check failed"
    assert state.firing()[0].reason == "a blocking quality check failed"

    passed = _signal(HealthState.HEALTHY, SignalKind.QUALITY, reason="quality checks passed")
    _, notifications = _run(state, [passed], 10)
    assert [(n.event, n.severity) for n in notifications] == [(AlertEvent.RESOLVED, AlertSeverity.CRITICAL)]


def test_quality_warn_alerts_as_warning_from_the_real_rule() -> None:
    signal = evaluate_quality(
        DEV, QualityObservation(QualityOutcome.WARN, observed_at=T0, run="run-1"), now=T0, max_age=MAX_AGE
    )
    _, notifications = _run(AlertState(), [signal], 0)
    assert [(n.kind, n.severity) for n in notifications] == [(AlertKind.QUALITY, AlertSeverity.WARNING)]


def test_canceled_runs_do_not_alert_or_resolve() -> None:
    canceled = evaluate_execution(DEV, RunObservation(RunStatus.CANCELED, T0, run="run-1"), now=T0, max_age=MAX_AGE)
    assert _run(AlertState(), [canceled], 0) == (AlertState(), [])
    state, _ = _run(AlertState(), [_failed()], 1)
    state, notifications = _run(state, [canceled], 2)
    assert (notifications, len(state.firing())) == ([], 1)


def test_a_stopped_schedule_resolves_missed_run_silently_and_records_it() -> None:
    state, _ = _run(AlertState(), [_missed()], 0)
    disabled = _missed(HealthState.DISABLED, "schedule is stopped")
    result = evaluate(state, [disabled], _at(5))

    assert (result.notifications, result.state.incidents) == ([], ())
    (event,) = result.events
    assert (event.event, event.incident.kind) == (AlertEvent.RESOLVED_SILENTLY, AlertKind.MISSED_RUN)


def test_disabled_holds_every_other_kind() -> None:
    state, _ = _replay([[_failed(), _dagster(HealthState.UNKNOWN)]] * UNREADABLE_THRESHOLD)
    disabled = [_signal(HealthState.DISABLED), _dagster(HealthState.DISABLED)]
    result = evaluate(state, disabled, _at(30))
    assert (result.notifications, result.events) == ([], [])
    assert len(result.state.firing()) == 2


@pytest.mark.parametrize("schedule_state", [ScheduleState.STOPPED, ScheduleState.ABSENT])
def test_stopped_or_absent_schedule_never_opens_missed_run(schedule_state: ScheduleState) -> None:
    observation = ScheduleObservation(schedule_state, T0, last_expected_at=T0 - timedelta(hours=1))
    signal = evaluate_expected_run(DEV, observation, now=T0, max_age=MAX_AGE, grace=GRACE)
    assert _run(AlertState(), [signal], 0) == (AlertState(), [])


def test_schedule_mismatch_is_a_warning_and_resolves_on_healthy() -> None:
    mismatch = _signal(HealthState.WARNING, SignalKind.EXPECTED_RUN, reason="schedule differs", run=None)
    state, notifications = _run(AlertState(), [mismatch], 0)
    assert [(n.kind, n.severity) for n in notifications] == [(AlertKind.SCHEDULE_MISMATCH, AlertSeverity.WARNING)]
    assert _events(_run(state, [_missed(HealthState.HEALTHY, STARTED)], 5)[1]) == [AlertEvent.RESOLVED]


def test_stale_output_alerts_and_other_signal_kinds_are_ignored() -> None:
    latest = _signal(HealthState.CRITICAL, SignalKind.LATEST_SUCCESS, run=None)
    trino = _signal(HealthState.CRITICAL, SignalKind.SERVICE, component="trino", run=None)
    _, notifications = _run(AlertState(), [_stale(), latest, trino], 0)
    assert [n.kind for n in notifications] == [AlertKind.STALE_OUTPUT]


@pytest.mark.parametrize(
    ("cause", "expected"), [(_failed, AlertKind.RUN_FAILED), (_missed, AlertKind.MISSED_RUN)], ids=["failed", "missed"]
)
def test_stale_output_is_suppressed_while_its_cause_is_firing_or_opening(cause, expected: AlertKind) -> None:
    _, notifications = _run(AlertState(), [_stale(), cause()], 0)
    assert [n.kind for n in notifications] == [expected]

    state, _ = _run(AlertState(), [cause()], 0)
    state, notifications = _run(state, [_stale()], 5)
    assert (notifications, len(state.firing())) == ([], 1)

    other_product = Scope(stage="dev", domain="sales", product="customers")
    _, notifications = _run(
        state, [_signal(HealthState.CRITICAL, SignalKind.FRESHNESS, other_product, "data is stale")], 10
    )
    assert [n.kind for n in notifications] == [AlertKind.STALE_OUTPUT]


def test_open_incidents_outside_the_active_scopes_resolve_silently() -> None:
    other = Scope(stage="dev", domain="sales", product="customers")
    state, _ = _run(AlertState(), [_failed(), _failed(other)], 0)

    result = evaluate(state, [], _at(5), active_scopes={Scope("dev", "sales", "customers")}, active_stages={"dev"})
    assert [(e.event, e.incident.scope.product) for e in result.events] == [(AlertEvent.RESOLVED_SILENTLY, "orders")]
    assert (result.notifications, [i.scope.product for i in result.state.incidents]) == ([], ["customers"])

    state, _ = _run(AlertState(), [_failed(DEV), _failed(PROD)], 0)
    result = evaluate(state, [_failed(PROD)], _at(5), active_stages={"dev"})
    assert [i.scope.stage for i in result.state.incidents] == ["dev"]
    assert (result.notifications, [e.event for e in result.events]) == ([], [AlertEvent.RESOLVED_SILENTLY])

    unknown = [_dagster(HealthState.UNKNOWN, "qa")]
    assert evaluate(AlertState(), unknown, _at(0), active_stages={"dev"}).state.unknown_counts == {}


def test_a_table_qualified_active_scope_keeps_its_incident() -> None:
    scope = Scope("dev", "sales", "orders", "silver.orders")
    state, _ = _run(AlertState(), [_failed(scope)], 0)
    result = evaluate(state, [], _at(5), active_scopes={scope}, active_stages={"dev"})
    assert (result.events, len(result.state.firing())) == ([], 1)


def test_state_keeps_open_incidents_only_and_the_outbox_ages() -> None:
    state, _ = _run(AlertState(), [_failed()], 0)
    state, (resolved,) = _run(state, [_healthy()], 5)
    assert state.incidents == ()
    assert [(entry.notification.id, entry.runs) for entry in state.outbox] == [
        (f"{resolved.incident_id}:opened", 1),
        (f"{resolved.incident_id}:resolved", 0),
    ]


def test_non_utc_now_is_normalised_and_naive_now_is_rejected() -> None:
    plus_two = datetime(2026, 11, 16, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    (notification,) = evaluate(AlertState(), [_failed()], plus_two).notifications
    assert notification.event_at.utcoffset() == timedelta(0)
    assert notification.payload()["opened_at"] == "2026-11-16T12:00:00+00:00"
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate(AlertState(), [], datetime(2026, 11, 16, 12, 0))


ADR_FIELDS = {
    "schema", "event", "incident_id", "condition", "severity", "stage", "domain", "product", "table",
    "run_id", "opened_at", "event_at", "summary", "links",
}  # fmt: skip


def _opening(kind: AlertKind) -> Signal:
    return {
        AlertKind.RUN_FAILED: _failed(),
        AlertKind.MISSED_RUN: _missed(),
        AlertKind.SCHEDULE_MISMATCH: _missed(HealthState.WARNING, "schedule does not match its descriptor"),
        AlertKind.STALE_OUTPUT: _stale(),
        AlertKind.QUALITY: _quality(HealthState.WARNING),
        AlertKind.STAGE_UNREADABLE: _dagster(HealthState.UNKNOWN),
        AlertKind.POLICY_UNREADABLE: _dagster(HealthState.WARNING, component=ALERT_POLICY),
    }[kind]


@pytest.mark.parametrize("kind", list(AlertKind))
def test_payload_of_every_condition_and_event_matches_the_adr_field_table(kind: AlertKind) -> None:
    hostile = Signal(**{**_opening(kind).__dict__, "reason": "password=hunter2", "evidence_uri": "token=abc"})
    repeat = UNREADABLE_THRESHOLD if kind is AlertKind.STAGE_UNREADABLE else 1
    state, sent = AlertState(), []
    for minutes in range(repeat):
        state, notifications = _run(state, [hostile], minutes, links=lambda i: {"run": f"https://d/{i.id}"})
        sent += notifications
    if kind is AlertKind.QUALITY:
        state, notifications = _run(state, [_quality(HealthState.CRITICAL)], 9)
        sent += notifications
    recovered = _signal(HealthState.HEALTHY, SignalKind.QUALITY, reason="quality checks passed")
    resolving = {
        AlertKind.RUN_FAILED: _healthy(),
        AlertKind.MISSED_RUN: _missed(HealthState.HEALTHY, STARTED),
        AlertKind.SCHEDULE_MISMATCH: _missed(HealthState.HEALTHY, STARTED),
        AlertKind.STALE_OUTPUT: _signal(HealthState.HEALTHY, SignalKind.FRESHNESS, run=None),
        AlertKind.QUALITY: recovered,
        AlertKind.STAGE_UNREADABLE: _dagster(HealthState.HEALTHY),
        AlertKind.POLICY_UNREADABLE: _dagster(HealthState.HEALTHY, component=ALERT_POLICY),
    }[kind]
    sent += _run(state, [resolving], 20)[1]

    assert AlertEvent.OPENED in _events(sent) and AlertEvent.RESOLVED in _events(sent)
    for notification in sent:
        payload = notification.payload()
        assert set(payload) <= ADR_FIELDS
        assert {"schema", "event", "incident_id", "condition", "severity", "stage", "opened_at", "event_at"} <= set(
            payload
        )
        assert (payload["schema"], payload["condition"]) == ("openlakeforge.alert.v1", kind.value)
        assert None not in payload.values()
        assert "hunter2" not in json.dumps(payload) and "token=abc" not in json.dumps(payload)


def test_payload_omits_absent_scope_run_and_links() -> None:
    _, sent = _replay([[_dagster(HealthState.UNKNOWN)]] * UNREADABLE_THRESHOLD)
    payload = sent[0].payload()
    assert set(payload) == {
        "schema", "event", "incident_id", "condition", "severity", "stage", "opened_at", "event_at", "summary",
    }  # fmt: skip
    assert payload["summary"] == "stage could not be read"


def test_incident_ids_stay_unique_when_the_state_is_lost() -> None:
    _, (first,) = _run(AlertState(), [_failed()], 0)
    _, (again,) = _run(AlertState(), [_failed()], 5)
    assert first.incident_id != again.incident_id
    assert first.incident_id == f"{fingerprint(DEV, AlertKind.RUN_FAILED)}-{int(T0.timestamp())}-1"
