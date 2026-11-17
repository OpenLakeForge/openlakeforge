"""Platform service alerts: `service_degraded` and `health_unreadable` (ADR 0023, decision 11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _alerts_support import evaluate
from openlakeforge_core import (
    PLATFORM_HEALTH,
    SERVICE_COMPONENTS,
    UNREADABLE_THRESHOLD,
    AlertEvent,
    AlertKind,
    AlertSeverity,
    AlertState,
    HealthState,
    Scope,
    service_signal,
)

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
WORKLOADS = "dagster.workloads"
SPOOL = "log-archive.spool-age"


def _signal(component: str, state: HealthState, stage: str = "dev", reason: str = "3 of 5 pods are not ready"):
    return service_signal(Scope(stage=stage), component, state, reason, now=T0, max_age=timedelta(minutes=5))


def _run(state: AlertState, signals, minutes: int):
    result = evaluate(state, signals, T0 + timedelta(minutes=5 * minutes))
    return result.state, result.notifications


def test_a_critical_component_opens_once_and_recovery_resolves_it() -> None:
    state, sent = _run(AlertState(), [_signal(WORKLOADS, HealthState.CRITICAL)], 0)
    assert [(n.event, n.kind, n.severity) for n in sent] == [
        (AlertEvent.OPENED, AlertKind.SERVICE_DEGRADED, AlertSeverity.CRITICAL)
    ]
    state, sent = _run(state, [_signal(WORKLOADS, HealthState.CRITICAL)], 1)
    assert sent == []
    # No reminder however long it stays open.
    for minutes in range(2, 30):
        state, sent = _run(state, [_signal(WORKLOADS, HealthState.CRITICAL)], minutes)
        assert sent == []

    state, sent = _run(state, [_signal(WORKLOADS, HealthState.HEALTHY)], 30)
    assert [n.event for n in sent] == [AlertEvent.RESOLVED]
    assert state.incidents == ()


def test_an_incident_is_one_per_stage_and_component() -> None:
    state, sent = _run(
        AlertState(),
        [
            _signal(WORKLOADS, HealthState.CRITICAL),
            _signal(WORKLOADS, HealthState.CRITICAL, stage="prod"),
            _signal("trino.workloads", HealthState.CRITICAL),
        ],
        0,
    )

    assert len(state.incidents) == 3
    assert {(i.scope.stage, i.component) for i in state.incidents} == {
        ("dev", WORKLOADS),
        ("prod", WORKLOADS),
        ("dev", "trino.workloads"),
    }
    assert len({n.payload()["summary"] for n in sent}) == 2


def test_workloads_alert_at_warning_and_the_other_components_at_critical_only() -> None:
    state, sent = _run(AlertState(), [_signal(WORKLOADS, HealthState.WARNING)], 0)
    assert [(n.kind, n.severity) for n in sent] == [(AlertKind.SERVICE_DEGRADED, AlertSeverity.WARNING)]

    for component in set(SERVICE_COMPONENTS) - {WORKLOADS, "trino.workloads"}:
        assert _run(AlertState(), [_signal(component, HealthState.WARNING)], 0)[1] == [], component


def test_a_workload_that_worsens_escalates_once() -> None:
    state, _ = _run(AlertState(), [_signal(WORKLOADS, HealthState.WARNING)], 0)

    state, sent = _run(state, [_signal(WORKLOADS, HealthState.CRITICAL)], 1)

    assert [(n.event, n.severity) for n in sent] == [(AlertEvent.ESCALATED, AlertSeverity.CRITICAL)]


@pytest.mark.parametrize("quiet", [HealthState.UNKNOWN, HealthState.DISABLED])
def test_unknown_and_disabled_neither_open_nor_resolve(quiet: HealthState) -> None:
    assert _run(AlertState(), [_signal(WORKLOADS, quiet)], 0) == (AlertState(), [])

    state, _ = _run(AlertState(), [_signal(WORKLOADS, HealthState.CRITICAL)], 0)
    kept, sent = _run(state, [_signal(WORKLOADS, quiet)], 1)

    assert sent == []
    assert [i.component for i in kept.incidents] == [WORKLOADS]


def test_a_component_the_record_does_not_name_never_alerts() -> None:
    for component in ("dagster.read-api", "polaris.readiness", "trino.readiness", "service-probes", "anything"):
        assert _run(AlertState(), [_signal(component, HealthState.CRITICAL)], 0)[1] == [], component


def test_the_notification_names_the_component_without_the_probes_text() -> None:
    _, (sent,) = _run(AlertState(), [_signal(SPOOL, HealthState.CRITICAL, reason="password=hunter2")], 0)

    assert sent.payload()["summary"] == "the log archive's spool is not draining"
    assert "hunter2" not in str(sent.payload())


def test_an_unreadable_api_opens_health_unreadable_after_three_reads_and_leaves_service_incidents_alone() -> None:
    state, _ = _run(AlertState(), [_signal(WORKLOADS, HealthState.CRITICAL)], 0)
    unknown = _signal(PLATFORM_HEALTH, HealthState.UNKNOWN, reason="the platform API answered HTTP 503")

    sent_all = []
    for minutes in range(1, UNREADABLE_THRESHOLD + 1):
        state, sent = _run(state, [unknown], minutes)
        sent_all += sent

    assert [(n.event, n.kind, n.severity) for n in sent_all] == [
        (AlertEvent.OPENED, AlertKind.HEALTH_UNREADABLE, AlertSeverity.CRITICAL)
    ]
    assert {i.kind for i in state.incidents} == {AlertKind.SERVICE_DEGRADED, AlertKind.HEALTH_UNREADABLE}

    state, sent = _run(state, [_signal(PLATFORM_HEALTH, HealthState.HEALTHY)], 10)
    # The API answered and says nothing about the component here: the incident stays, the unreadable one resolves.
    assert [(n.event, n.kind) for n in sent] == [(AlertEvent.RESOLVED, AlertKind.HEALTH_UNREADABLE)]
    assert [i.kind for i in state.incidents] == [AlertKind.SERVICE_DEGRADED]


def test_health_unreadable_and_stage_unreadable_keep_separate_counters() -> None:
    from openlakeforge_core import DAGSTER_READ_API

    signals = [
        _signal(PLATFORM_HEALTH, HealthState.UNKNOWN),
        _signal(DAGSTER_READ_API, HealthState.UNKNOWN),
    ]
    state, _ = _run(AlertState(), signals, 0)

    assert set(state.unknown_counts) == {"dev", "dev:health_unreadable"}
