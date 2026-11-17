"""Who the evaluator tells: a rule's routes, the receivers' stage ceiling and the platform receivers."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from _alerting_fakes import FakeBucket, publish
from test_alerting import NOW, FakeDagster, FakeNotifier, _evaluate, _failed, _settings, _told

from openlakeforge_platform_api.alerting.settings import ReceiverSetting

OPS = ReceiverSetting("ops", "OPS_URL")
CHAT = ReceiverSetting("chat", "CHAT_URL")


def _policy(notify: str, *, stages: str = "[dev, prod]", routes: str = "on-call: ops, sales: chat") -> str:
    return f"""\
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy
routes: {{{routes}}}
rules:
  - match: {{domain: "*", product: "*"}}
    stages: {stages}
    notify: [{notify}]
    when:
      run_failed: {{}}
"""


def _run(policy: str, *receivers: ReceiverSetting, stages: tuple[str, ...] = ("dev",)):
    bucket = FakeBucket()
    for stage in stages:
        publish(bucket, stage=stage, alerts=policy)
    notifiers = {r.name: FakeNotifier() for r in receivers}
    dagster = {stage: FakeDagster([_failed()]) for stage in stages}
    _evaluate(_settings(*receivers, stages=stages), bucket, dagster, notifiers)
    return {name: _told(n) for name, n in notifiers.items()}, bucket, dagster


def test_a_rule_notifies_the_receivers_its_routes_name_and_no_others() -> None:
    told, *_ = _run(_policy("sales"), OPS, CHAT)

    assert told == {"ops": [], "chat": [("dev", "run_failed", "opened")]}


def test_several_routes_notify_each_receiver_once() -> None:
    told, *_ = _run(_policy("on-call, sales"), OPS, CHAT)

    assert told["ops"] == told["chat"] == [("dev", "run_failed", "opened")]


def test_a_receivers_stages_are_a_ceiling_a_rule_cannot_raise() -> None:
    chat = ReceiverSetting("chat", "CHAT_URL", ("prod",))
    told, bucket, _ = _run(_policy("on-call, sales"), OPS, chat, stages=("dev", "prod"))

    assert sorted(told["ops"]) == [("dev", "run_failed", "opened"), ("prod", "run_failed", "opened")]
    assert told["chat"] == [("prod", "run_failed", "opened")]
    assert bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["outbox"] == []


def test_a_route_whose_receiver_is_gone_is_skipped_logged_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    policy = _policy("on-call, sales", routes="on-call: ops, sales: gone")

    with caplog.at_level(logging.WARNING):
        told, *_ = _run(policy, OPS)

    assert ("dev", "run_failed", "opened") in told["ops"]
    assert ("dev", "policy_unreadable", "opened") in told["ops"]
    assert any("receiver gone" in record.getMessage() for record in caplog.records)


def test_platform_conditions_go_only_to_the_platform_receivers_whatever_alerts_yml_routes() -> None:
    told, *_ = _run(
        _policy("sales", routes="on-call: ops, sales: chat, gone: absent"),
        OPS,
        ReceiverSetting("chat", "CHAT_URL", (), False),
    )

    # The policy names a receiver the profile lacks, so the stage raises policy_unreadable: only to "ops".
    assert [c for _, c, _ in told["ops"]] == ["policy_unreadable"]
    assert [c for _, c, _ in told["chat"]] == ["run_failed"]


def test_every_receiver_gets_platform_conditions_unless_the_profile_narrows_them() -> None:
    told, *_ = _run("kind: not a policy\n", OPS, CHAT)

    assert told["ops"] == told["chat"] == [("dev", "policy_unreadable", "opened")]


def test_an_unreadable_stage_is_told_only_to_the_platform_receivers() -> None:
    bucket = FakeBucket()
    publish(bucket, alerts=_policy("sales"))
    chat = ReceiverSetting("chat", "CHAT_URL", (), False)
    notifiers = {"ops": FakeNotifier(), "chat": FakeNotifier()}
    dagster = {"dev": FakeDagster()}
    dagster["dev"].unreadable = True

    for minutes in range(3):
        _evaluate(_settings(OPS, chat), bucket, dagster, notifiers, NOW + timedelta(minutes=5 * minutes))

    assert _told(notifiers["ops"]) == [("dev", "stage_unreadable", "opened")]
    assert notifiers["chat"].sent == []


def test_the_policy_clearing_resolves_policy_unreadable_to_the_same_receivers() -> None:
    bucket = FakeBucket()
    publish(bucket, alerts="kind: not a policy\n")
    ops, dagster = FakeNotifier(), {"dev": FakeDagster()}
    _evaluate(_settings(OPS), bucket, dagster, {"ops": ops})

    publish(bucket, alerts=_policy("on-call", routes="on-call: ops"), seed="b")
    _evaluate(_settings(OPS), bucket, dagster, {"ops": ops}, NOW + timedelta(minutes=5))

    assert _told(ops) == [("dev", "policy_unreadable", "opened"), ("dev", "policy_unreadable", "resolved")]


def _queued(bucket: FakeBucket) -> list[str]:
    outbox = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["outbox"]
    return [entry["notification"]["id"] for entry in outbox]


def test_a_queued_notification_waits_while_its_stage_policy_cannot_be_read_at_all() -> None:
    bucket = FakeBucket()
    publish(bucket, alerts=_policy("on-call", routes="on-call: ops"))
    ops, dagster, settings = FakeNotifier(up=False), {"dev": FakeDagster([_failed()])}, _settings(OPS)

    _evaluate(settings, bucket, dagster, {"ops": ops})
    (queued,) = _queued(bucket)
    # Neither the stage's pointer nor the cached policy can be read this time.
    pointer = bucket.objects["activations/dev/ACTIVE.json"]
    del bucket.objects["alerts/state/v1/policy/dev.json"]
    bucket.objects["activations/dev/ACTIVE.json"] = b"\xff\xfe"
    ops.up = True

    _evaluate(settings, bucket, dagster, {"ops": ops}, NOW + timedelta(minutes=5))

    assert queued in _queued(bucket)
    assert ("dev", "policy_unreadable", "opened") in _told(ops)  # platform conditions do not need the policy
    # The one attempt that failed is the only send of it so far.
    assert _told(ops).count(("dev", "run_failed", "opened")) == 1

    bucket.objects["activations/dev/ACTIVE.json"] = pointer
    _evaluate(settings, bucket, dagster, {"ops": ops}, NOW + timedelta(minutes=10))

    assert queued not in _queued(bucket)
    assert _told(ops).count(("dev", "run_failed", "opened")) == 2


def test_a_notification_whose_policy_was_read_and_routes_nobody_still_leaves_the_outbox() -> None:
    bucket = FakeBucket()
    publish(bucket, alerts=_policy("sales", routes="on-call: ops, sales: gone"))

    _evaluate(_settings(OPS), bucket, {"dev": FakeDagster([_failed()])}, {"ops": FakeNotifier(up=False)})

    assert all(_condition(bucket, queued) != "run_failed" for queued in _queued(bucket))


def _condition(bucket: FakeBucket, notification_id: str) -> str:
    outbox = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["outbox"]
    return next(e["notification"]["kind"] for e in outbox if e["notification"]["id"] == notification_id)
