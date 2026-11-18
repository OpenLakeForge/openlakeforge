from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from _alerts_support import evaluate
from openlakeforge_core import (
    DAGSTER_READ_API,
    MAX_OUTBOX_RUNS,
    AlertEvent,
    AlertState,
    AlertStateError,
    Delivery,
    DeliveryOutcome,
    HealthState,
    Scope,
    Signal,
    SignalKind,
    drop_notification,
    expire_outbox,
    record_attempt,
    record_delivery,
    service_signal,
    state_from_json,
    state_to_json,
)

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
FAILED = Signal(
    kind=SignalKind.EXECUTION,
    state=HealthState.CRITICAL,
    stage="dev",
    reason="latest run failed",
    domain="sales",
    product="orders",
    table="silver.orders",
    run="run-1",
)
HEALTHY = Signal(**{**FAILED.__dict__, "state": HealthState.HEALTHY, "reason": "latest run succeeded"})


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _queued() -> tuple[AlertState, str]:
    result = evaluate(AlertState(), [FAILED], T0)
    return result.state, result.notifications[0].id


def test_a_notification_stays_queued_until_every_routed_receiver_has_it() -> None:
    state, notification_id = _queued()
    routed = {"ops", "chat"}

    state = record_delivery(state, notification_id, "ops", routed=routed)
    assert state.outbox[0].delivered == {"ops"}

    state = record_delivery(state, notification_id, "chat", routed=routed)
    assert state.outbox == ()


def test_a_receiver_outside_the_routing_does_not_complete_delivery() -> None:
    state, notification_id = _queued()
    state = record_delivery(state, notification_id, "other", routed={"ops"})
    assert [entry.notification.id for entry in state.outbox] == [notification_id]


def test_attempts_are_counted_per_receiver_and_a_notification_can_be_dropped() -> None:
    state, notification_id = _queued()
    state = record_attempt(record_attempt(state, notification_id, "ops"), notification_id, "ops")
    state = record_attempt(state, notification_id, "chat")
    assert state.outbox[0].attempts == {"ops": 2, "chat": 1}
    assert drop_notification(state, notification_id).outbox == ()


def test_outbox_entries_expire_after_twelve_evaluations_and_are_returned() -> None:
    state, notification_id = _queued()
    for minutes in range(1, MAX_OUTBOX_RUNS):
        state = evaluate(state, [], _at(5 * minutes)).state
        kept, expired = expire_outbox(state)
        assert (kept, expired) == (state, [])

    state = evaluate(state, [], _at(60)).state
    kept, expired = expire_outbox(state)
    assert kept.outbox == ()
    assert [entry.notification.id for entry in expired] == [notification_id]


def test_a_flapping_incident_does_not_grow_the_state_without_bound() -> None:
    state = AlertState()
    for cycle in range(200):
        state = evaluate(state, [FAILED if cycle % 2 == 0 else HEALTHY], _at(5 * cycle)).state
        state, _ = expire_outbox(state)
    assert len(state.outbox) <= MAX_OUTBOX_RUNS
    assert len(state.incidents) <= 1
    assert len(json.loads(state_to_json(state))["incidents"]) <= 1


def test_state_round_trips_through_json() -> None:
    dagster = service_signal(
        Scope(stage="dev"), DAGSTER_READ_API, HealthState.UNKNOWN, "no observation", now=T0, max_age=timedelta(hours=1)
    )
    result = evaluate(AlertState(), [FAILED, dagster], T0, links=lambda _: {"docs": "https://docs.example/a"})
    state = record_delivery(
        record_attempt(result.state, result.notifications[0].id, "ops"),
        result.notifications[0].id,
        "ops",
        routed={"ops", "chat"},
    )
    state = evaluate(state, [HEALTHY], _at(5)).state

    restored = state_from_json(state_to_json(state))

    assert restored == state
    assert state_to_json(restored) == state_to_json(state)
    assert json.loads(state_to_json(state))["schema_version"] == 1


def test_a_settled_notification_leaves_its_outcome_on_the_open_incident() -> None:
    state, notification_id = _queued()
    state = record_attempt(record_attempt(state, notification_id, "ops"), notification_id, "ops")
    state = record_delivery(state, notification_id, "ops", routed={"ops"})

    assert state.outbox == ()
    assert state.incidents[0].delivery == Delivery(
        event=AlertEvent.OPENED, outcome=DeliveryOutcome.DELIVERED, receivers=1, delivered=1, attempts=2
    )


def test_a_refusal_by_one_receiver_makes_the_notification_undeliverable() -> None:
    state, notification_id = _queued()
    routed = {"ops", "chat"}
    for name in sorted(routed):
        state = record_attempt(state, notification_id, name)
    state = record_delivery(state, notification_id, "chat", routed=routed, rejected=True)
    assert state_from_json(state_to_json(state)) == state
    state = record_delivery(state, notification_id, "ops", routed=routed)

    assert state.outbox == ()
    assert state_from_json(state_to_json(state)) == state
    assert state.incidents[0].delivery == Delivery(
        event=AlertEvent.OPENED, outcome=DeliveryOutcome.UNDELIVERABLE, receivers=2, delivered=1, attempts=2
    )


def test_an_expired_notification_is_undeliverable_and_a_dropped_one_unrouted() -> None:
    state, notification_id = _queued()
    dropped = drop_notification(state, notification_id)
    assert dropped.incidents[0].delivery is not None
    assert dropped.incidents[0].delivery.outcome is DeliveryOutcome.UNROUTED

    state = record_attempt(state, notification_id, "ops")
    for minutes in range(1, MAX_OUTBOX_RUNS + 1):
        state = evaluate(state, [FAILED], _at(5 * minutes)).state
    state, expired = expire_outbox(state)

    assert [entry.notification.id for entry in expired] == [notification_id]
    assert state.incidents[0].delivery == Delivery(
        event=AlertEvent.OPENED, outcome=DeliveryOutcome.UNDELIVERABLE, receivers=1, delivered=0, attempts=1
    )


def test_a_state_written_before_outcomes_were_kept_still_reads() -> None:
    document = _document()
    del document["incidents"][0]["delivery"]
    del document["outbox"][0]["rejected"]

    restored = state_from_json(json.dumps(document))

    assert restored.incidents[0].delivery is None
    assert restored.outbox[0].rejected == frozenset()


def _document() -> dict:
    state = evaluate(AlertState(), [FAILED], T0).state
    return json.loads(state_to_json(state))


def _with(path: list, value: object) -> str:
    document = _document()
    target = document
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return json.dumps(document)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"schema_version": 1}',
        '{"schema_version": 1, "incidents": [{}], "outbox": [], "unknown_counts": {}, "sequences": {}}',
        _with(["schema_version"], True),
        _with(["schema_version"], 2),
        _with(["incidents", 0, "run_ids"], "abc"),
        _with(["incidents", 0, "run_ids"], [1]),
        _with(["incidents", 0, "opened_at"], "2026-11-16T14:00:00+02:00"),
        _with(["incidents", 0, "opened_at"], "2026-11-16T12:00:00"),
        _with(["incidents", 0, "kind"], "nope"),
        _with(["unknown_counts"], {"dev": -1}),
        _with(["unknown_counts"], {"dev": True}),
        _with(["unknown_counts"], {"dev": 1.0}),
        _with(["sequences"], {"abc": -2}),
        _with(["outbox", 0, "runs"], -1),
        _with(["outbox", 0, "runs"], True),
        _with(["outbox", 0, "delivered"], "ops"),
        _with(["outbox", 0, "attempts"], {"ops": "2"}),
        _with(["outbox", 0, "notification", "links"], [["only-one"]]),
        _with(["outbox", 0, "notification", "event"], "resolved_silently"),
        _with(["outbox", 0, "rejected"], "ops"),
        _with(
            ["incidents", 0, "delivery"],
            {"event": "opened", "outcome": "lost", "receivers": 1, "delivered": 0, "attempts": 1},
        ),
        _with(
            ["incidents", 0, "delivery"],
            {"event": "opened", "outcome": "delivered", "receivers": -1, "delivered": 0, "attempts": 1},
        ),
    ],
)
def test_malformed_state_is_refused(text: str) -> None:
    with pytest.raises(AlertStateError):
        state_from_json(text)
