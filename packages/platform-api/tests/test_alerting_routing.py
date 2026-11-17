"""Who the evaluator tells: a rule's routes, the receivers' stage ceiling and the platform receivers."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from _alerting_fakes import FakeBucket, publish, settings_mapping
from openlakeforge_core import AlertPolicy, parse_alert_policy
from test_alerting import BASE, NOW, FakeDagster, FakeNotifier, _evaluate, _failed, _settings, _told

from openlakeforge_platform_api.alerting import __main__ as entrypoint
from openlakeforge_platform_api.alerting.run import run_once
from openlakeforge_platform_api.alerting.settings import ReceiverSetting
from openlakeforge_platform_api.alerting.store import AlertStore

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


def _override(when: str, notify: str = "sales") -> AlertPolicy:
    return parse_alert_policy(
        {
            "apiVersion": "openlakeforge.io/v1beta1",
            "kind": "AlertPolicy",
            "routes": {"on-call": "ops", "sales": "chat"},
            "rules": [{"match": {"domain": "*", "product": "*"}, "notify": [notify], "when": {when: {}}}],
        }
    )


def test_a_policy_override_replaces_the_revisions_rules_and_routes() -> None:
    bucket = FakeBucket()
    publish(bucket, alerts=_policy("on-call"))
    ops, chat = FakeNotifier(), FakeNotifier()
    settings = _settings(OPS, CHAT)

    run_once(
        settings=settings,
        store=AlertStore(bucket, BASE),
        dagster={"dev": FakeDagster([_failed()])},
        notifiers={"ops": ops, "chat": chat},
        now=NOW,
        policy_override=_override("run_failed"),
    )

    assert (_told(ops), _told(chat)) == ([], [("dev", "run_failed", "opened")])


def test_a_policy_override_that_does_not_declare_a_condition_silences_it_whatever_the_revision_says() -> None:
    bucket = FakeBucket()
    publish(bucket, alerts=_policy("on-call"))
    ops = FakeNotifier()

    run_once(
        settings=_settings(OPS, CHAT),
        store=AlertStore(bucket, BASE),
        dagster={"dev": FakeDagster([_failed()])},
        notifiers={"ops": ops, "chat": FakeNotifier()},
        now=NOW,
        policy_override=_override("quality", "on-call"),
    )

    assert ops.sent == []


def test_the_entrypoint_takes_a_policy_file_and_refuses_one_that_is_not_a_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = tmp_path / "alerts.json"
    settings.write_text(json.dumps(settings_mapping()))
    monkeypatch.setenv("OPS_URL", "https://hooks.example.test/x")
    broken = tmp_path / "alerts.yml"
    broken.write_text("kind: not a policy\n")

    assert entrypoint.main(["--settings", str(settings), "--policy-file", str(broken)]) == 2
