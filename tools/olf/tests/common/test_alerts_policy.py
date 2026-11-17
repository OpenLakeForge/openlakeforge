"""`evaluate` under the alert policy: silence, precedence of options, severity and what a rule change does."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _alerts_support import ALL_ON, evaluate, policy_for
from openlakeforge_core import (
    AlertEvent,
    AlertKind,
    AlertSeverity,
    AlertState,
    HealthState,
    QualityOutcome,
    ResolvedPolicy,
    Scope,
    Signal,
    SignalKind,
    evaluate_freshness_age,
    evaluate_quality,
    service_signal,
)
from openlakeforge_core.alerts import Notification
from openlakeforge_core.health import QualityObservation, RunObservation, RunStatus

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
ORDERS = Scope(stage="prod", product="orders_pipeline")
REFUNDS = Scope(stage="prod", product="refunds_pipeline")
MAINTENANCE = Scope(stage="prod", product="gold_maintenance")
EVERYTHING = {"match": {"domain": "*", "product": "*"}, "when": {"run_failed": {}, "quality": {}, "freshness": {}}}


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _failed(scope: Scope = ORDERS) -> Signal:
    return Signal(SignalKind.EXECUTION, HealthState.CRITICAL, scope.stage, "latest run failed", product=scope.product)


def _succeeded(scope: Scope = ORDERS) -> Signal:
    return Signal(SignalKind.EXECUTION, HealthState.HEALTHY, scope.stage, "latest run succeeded", product=scope.product)


def _quality(outcome: QualityOutcome, *, blocking: bool = True, scope: Scope = ORDERS) -> Signal:
    return evaluate_quality(
        scope, QualityObservation(outcome, T0, blocking=blocking), now=T0, max_age=timedelta(hours=1)
    )


def _age(hours: float, scope: Scope = ORDERS) -> Signal:
    success = RunObservation(RunStatus.SUCCEEDED, T0 - timedelta(hours=hours), run="run-1")
    return evaluate_freshness_age(
        scope, success, now=T0, warn_after=timedelta(hours=26), critical_after=timedelta(hours=30)
    )


def _firing(signals: list[Signal], policy, state: AlertState | None = None, minutes: int = 0):
    result = evaluate(state or AlertState(), signals, _at(minutes), policy=policy)
    return result.state, result.notifications


def test_without_a_declaring_rule_nothing_alerts() -> None:
    state, sent = _firing(
        [_failed()],
        policy_for({"match": {"domain": "*", "product": "*"}, "stages": ["dev"], "when": {"run_failed": {}}}),
    )

    assert (state.incidents, sent) == ((), [])


def test_a_stage_with_an_empty_policy_is_silent() -> None:
    assert _firing([_failed()], {"prod": ResolvedPolicy(stage="prod")})[1] == []


def test_a_condition_a_rule_does_not_name_stays_silent() -> None:
    policy = policy_for({"match": {"domain": "*", "product": "*"}, "when": {"quality": {}}})

    assert _firing([_failed()], policy)[1] == []
    assert [n.kind for n in _firing([_quality(QualityOutcome.ERROR)], policy)[1]] == [AlertKind.QUALITY]


def test_the_most_specific_rule_decides_per_condition() -> None:
    policy = policy_for(
        EVERYTHING,
        {"match": {"domain": "sales", "product": "orders"}, "when": {"run_failed": {"severity": "warning"}}},
        {"match": {"domain": "sales", "product": "refunds"}, "when": {"run_failed": False}},
    )

    assert [n.severity for n in _firing([_failed(ORDERS)], policy)[1]] == [AlertSeverity.WARNING]
    assert _firing([_failed(REFUNDS)], policy)[1] == []
    assert [n.severity for n in _firing([_failed(MAINTENANCE)], policy)[1]] == [AlertSeverity.CRITICAL]


def test_a_job_of_no_product_is_matched_by_global_rules_only() -> None:
    policy = policy_for({"match": {"domain": "sales", "product": "orders"}, "when": {"run_failed": {}}})

    assert _firing([_failed(MAINTENANCE)], policy)[1] == []


@pytest.mark.parametrize(
    ("level", "outcome", "blocking", "expected"),
    [
        ("warn", QualityOutcome.WARN, True, AlertSeverity.WARNING),
        ("warn", QualityOutcome.ERROR, False, AlertSeverity.WARNING),
        ("warn", QualityOutcome.ERROR, True, AlertSeverity.CRITICAL),
        ("error", QualityOutcome.ERROR, False, AlertSeverity.WARNING),
        ("error", QualityOutcome.ERROR, True, AlertSeverity.CRITICAL),
    ],
)
def test_quality_signals_of_every_outcome_alert_at_their_severity(level, outcome, blocking, expected) -> None:
    policy = policy_for({"match": {"domain": "*", "product": "*"}, "when": {"quality": {"level": level}}})

    sent = _firing([_quality(outcome, blocking=blocking)], policy)[1]

    assert [n.severity for n in sent] == ([expected] if expected else [])


def test_severity_option_sets_missed_run_and_schedule_mismatch() -> None:
    policy = policy_for(
        {
            "match": {"domain": "*", "product": "*"},
            "when": {"missed_run": {"severity": "warning"}, "schedule_mismatch": {"severity": "critical"}},
        }
    )
    missed = Signal(
        SignalKind.EXPECTED_RUN, HealthState.CRITICAL, "prod", "an expected run did not start", product=ORDERS.product
    )
    mismatch = Signal(
        SignalKind.EXPECTED_RUN,
        HealthState.WARNING,
        "prod",
        "schedule is stopped but its cadence declares it running",
        product=ORDERS.product,
    )

    sent = _firing([missed, mismatch], policy)[1]

    assert {n.kind: n.severity for n in sent} == {
        AlertKind.MISSED_RUN: AlertSeverity.WARNING,
        AlertKind.SCHEDULE_MISMATCH: AlertSeverity.CRITICAL,
    }


def test_freshness_thresholds_warn_then_escalate_once_and_recover() -> None:
    policy = policy_for(
        {
            "match": {"domain": "*", "product": "*"},
            "when": {"freshness": {"warn_after": "26h", "critical_after": "30h"}},
        }
    )
    state, sent = _firing([_age(20)], policy)
    assert sent == []

    state, sent = _firing([_age(27)], policy, state, 5)
    assert [(n.event, n.severity, n.kind) for n in sent] == [
        (AlertEvent.OPENED, AlertSeverity.WARNING, AlertKind.STALE_OUTPUT)
    ]
    state, sent = _firing([_age(31)], policy, state, 10)
    assert [(n.event, n.severity) for n in sent] == [(AlertEvent.ESCALATED, AlertSeverity.CRITICAL)]
    state, sent = _firing([_age(27)], policy, state, 15)
    assert sent == []
    assert state.incidents[0].severity is AlertSeverity.WARNING
    state, sent = _firing([_age(1)], policy, state, 20)
    assert [n.event for n in sent] == [AlertEvent.RESOLVED]


def _due_signal(reason: str, state: HealthState, due: datetime) -> Signal:
    return Signal(SignalKind.EXPECTED_RUN, state, "prod", reason, product=ORDERS.product, due=due)


def test_raising_the_grace_of_an_open_missed_run_resolves_it_but_a_new_occurrence_does_not() -> None:
    policy = policy_for({"match": {"domain": "*", "product": "*"}, "when": {"missed_run": {}}})
    due = T0 - timedelta(minutes=20)
    state, sent = _firing([_due_signal("an expected run did not start", HealthState.CRITICAL, due)], policy)
    assert [n.event for n in sent] == [AlertEvent.OPENED]
    within = "the expected run is within its grace period"

    # Tomorrow's occurrence came due after the incident opened: an ordinary within-grace read clears nothing.
    tomorrow = T0 + timedelta(hours=24)
    held, sent = _firing([_due_signal(within, HealthState.HEALTHY, tomorrow)], policy, state, 24 * 60)
    assert (sent, [i.kind for i in held.incidents]) == ([], [AlertKind.MISSED_RUN])

    # The same occurrence reads as inside its grace because a rule raised the grace.
    state, sent = _firing([_due_signal(within, HealthState.HEALTHY, due)], policy, state, 5)
    assert [n.event for n in sent] == [AlertEvent.RESOLVED]
    assert state.incidents == ()


def test_cadence_staleness_takes_its_severity_from_the_rule() -> None:
    policy = policy_for({"match": {"domain": "*", "product": "*"}, "when": {"freshness": {"severity": "warning"}}})
    stale = Signal(
        SignalKind.FRESHNESS,
        HealthState.CRITICAL,
        "prod",
        "no run succeeded since the previous due time plus its grace",
        product=ORDERS.product,
    )

    assert [n.severity for n in _firing([stale], policy)[1]] == [AlertSeverity.WARNING]


def test_a_rule_that_disappears_resolves_its_incident_silently() -> None:
    state, sent = _firing([_failed()], ALL_ON)
    assert len(sent) == 1
    quiet = policy_for({"match": {"domain": "*", "product": "*"}, "when": {"quality": {}}})

    result = evaluate(state, [_failed()], _at(5), policy=quiet)

    assert result.notifications == []
    assert [e.event for e in result.events] == [AlertEvent.RESOLVED_SILENTLY]
    assert result.state.incidents == ()


def test_a_rule_that_comes_back_opens_a_new_incident_with_the_same_fingerprint() -> None:
    state, first = _firing([_failed()], ALL_ON)
    quiet = policy_for({"match": {"domain": "*", "product": "*"}, "when": {"quality": {}}})
    state, _ = _firing([_failed()], quiet, state, 5)

    state, second = _firing([_failed()], ALL_ON, state, 10)

    assert len(second) == 1
    assert second[0].incident_id != first[0].incident_id
    assert second[0].incident_id.split("-")[0] == first[0].incident_id.split("-")[0]


def test_a_severity_changed_by_the_rule_is_re_evaluated_on_the_next_run() -> None:
    warning = policy_for({"match": {"domain": "*", "product": "*"}, "when": {"run_failed": {"severity": "warning"}}})
    state, sent = _firing([_failed()], warning)
    assert [n.severity for n in sent] == [AlertSeverity.WARNING]

    state, sent = _firing([_failed()], ALL_ON, state, 5)
    assert [(n.event, n.severity) for n in sent] == [(AlertEvent.ESCALATED, AlertSeverity.CRITICAL)]

    state, sent = _firing([_failed()], warning, state, 10)
    assert sent == []
    assert state.incidents[0].severity is AlertSeverity.WARNING


def test_a_stage_without_a_policy_holds_its_open_incidents_and_opens_nothing() -> None:
    state, _ = _firing([_failed()], ALL_ON)

    held = evaluate(state, [_failed(REFUNDS)], _at(5), policy={})

    assert held.notifications == []
    assert [i.scope.product for i in held.state.incidents] == ["orders_pipeline"]


def test_the_stage_unreadable_condition_is_not_governed_by_the_policy() -> None:
    unreadable = [
        service_signal(Scope(stage="prod"), "dagster.read-api", HealthState.UNKNOWN, "x", now=T0, max_age=timedelta(1))
    ]
    state = AlertState()
    for minute in range(3):
        state = evaluate(state, unreadable, _at(minute * 5), policy={"prod": ResolvedPolicy(stage="prod")}).state

    assert [i.kind for i in state.incidents] == [AlertKind.STAGE_UNREADABLE]


def test_a_severity_move_without_a_notification_is_history_only() -> None:
    policy = policy_for(
        {
            "match": {"domain": "*", "product": "*"},
            "when": {"freshness": {"warn_after": "26h", "critical_after": "30h"}},
        }
    )
    state = AlertState()
    seen = []
    for minutes, hours in [(0, 31), (5, 27), (10, 31), (15, 27)]:
        result = evaluate(state, [_age(hours)], _at(minutes), policy=policy)
        state = result.state
        seen.append(([n.event for n in result.notifications], [e.event for e in result.events]))

    assert seen == [
        ([AlertEvent.OPENED], [AlertEvent.OPENED]),
        ([], [AlertEvent.SEVERITY_CHANGED]),
        ([], [AlertEvent.SEVERITY_CHANGED]),
        ([], [AlertEvent.SEVERITY_CHANGED]),
    ]


def test_a_severity_changed_event_cannot_become_a_notification() -> None:
    with pytest.raises(ValueError, match="severity_changed"):
        Notification(
            "n",
            AlertEvent.SEVERITY_CHANGED,
            "i",
            AlertSeverity.WARNING,
            AlertKind.RUN_FAILED,
            ORDERS,
            "r",
            _at(0),
            _at(0),
        )
