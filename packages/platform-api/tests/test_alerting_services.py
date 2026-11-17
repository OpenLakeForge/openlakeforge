"""Platform service alerts through the evaluator: stage health read from the platform API (ADR 0023, decision 11)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from _alerting_fakes import FakeBucket, declare_everything, publish
from openlakeforge_core import HealthState
from test_alerting import BASE, NOW, FakeDagster, FakeNotifier, _settings

from openlakeforge_platform_api.alerting.run import run_once
from openlakeforge_platform_api.alerting.service_health import (
    HealthReadError,
    ServiceState,
    StageHealthReader,
    stage_health_reader,
)
from openlakeforge_platform_api.alerting.settings import ReceiverSetting
from openlakeforge_platform_api.alerting.store import AlertStore

OPS = ReceiverSetting("ops", "OPS_URL", platform=False)
ON_CALL = ReceiverSetting("on-call", "ON_CALL_URL", platform=True)


class FakeHealth:
    def __init__(self, services: list[ServiceState] | None = None) -> None:
        self.services = services or []
        self.down = False
        self.asked: list[str] = []

    def __call__(self, stage: str) -> list[ServiceState]:
        self.asked.append(stage)
        if self.down:
            raise HealthReadError("the platform API answered HTTP 503")
        return self.services


def _service(component: str, state: HealthState) -> ServiceState:
    return ServiceState(component, state, "3 of 5 pods are not ready")


def _evaluate(health: StageHealthReader, bucket: FakeBucket, notifiers: dict[str, FakeNotifier], minutes: int = 0):
    settings = _settings(OPS, ON_CALL)
    declare_everything(bucket, settings.stages, [r.name for r in settings.receivers])
    return run_once(
        settings=settings,
        store=AlertStore(bucket, BASE),
        dagster={"dev": FakeDagster()},
        notifiers=notifiers,
        now=NOW + timedelta(minutes=5 * minutes),
        health=health,
    )


def _conditions(notifier: FakeNotifier) -> list[tuple[str, str]]:
    return [(p["condition"], p["event"]) for _, p in notifier.sent]


def test_a_critical_component_opens_and_recovery_resolves_it_for_platform_receivers_only() -> None:
    bucket, ops, on_call = FakeBucket(), FakeNotifier(), FakeNotifier()
    publish(bucket)
    health = FakeHealth(
        [_service("trino.workloads", HealthState.CRITICAL), _service("dagster.read-api", HealthState.CRITICAL)]
    )

    _evaluate(health, bucket, {"ops": ops, "on-call": on_call})
    _evaluate(health, bucket, {"ops": ops, "on-call": on_call}, 1)
    health.services = [_service("trino.workloads", HealthState.HEALTHY)]
    _evaluate(health, bucket, {"ops": ops, "on-call": on_call}, 2)

    assert _conditions(on_call) == [("service_degraded", "opened"), ("service_degraded", "resolved")]
    assert ops.sent == []


def test_an_unknown_component_changes_nothing() -> None:
    bucket, ops, on_call = FakeBucket(), FakeNotifier(), FakeNotifier()
    publish(bucket)
    health = FakeHealth([_service("trino.workloads", HealthState.CRITICAL)])
    _evaluate(health, bucket, {"ops": ops, "on-call": on_call})

    health.services = [
        _service("trino.workloads", HealthState.UNKNOWN),
        _service("object-store.list", HealthState.DISABLED),
    ]
    _evaluate(health, bucket, {"ops": ops, "on-call": on_call}, 1)

    assert _conditions(on_call) == [("service_degraded", "opened")]
    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert [i["kind"] for i in state["incidents"]] == ["service_degraded"]


def test_an_unreadable_api_opens_health_unreadable_and_leaves_open_service_incidents_alone() -> None:
    bucket, ops, on_call = FakeBucket(), FakeNotifier(), FakeNotifier()
    publish(bucket)
    health = FakeHealth([_service("dagster.workloads", HealthState.CRITICAL)])
    notifiers = {"ops": ops, "on-call": on_call}
    _evaluate(health, bucket, notifiers)

    health.down = True
    for minutes in (1, 2, 3):
        _evaluate(health, bucket, notifiers, minutes)
    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert sorted(i["kind"] for i in state["incidents"]) == ["health_unreadable", "service_degraded"]
    assert _conditions(on_call) == [("service_degraded", "opened"), ("health_unreadable", "opened")]

    health.down = False
    health.services = [_service("dagster.workloads", HealthState.CRITICAL)]
    _evaluate(health, bucket, notifiers, 4)

    assert _conditions(on_call)[-1] == ("health_unreadable", "resolved")
    assert ("service_degraded", "resolved") not in _conditions(on_call)
    assert ops.sent == []


def test_without_a_health_reader_no_service_condition_is_read() -> None:
    bucket, ops, on_call = FakeBucket(), FakeNotifier(), FakeNotifier()
    publish(bucket)
    settings = _settings(OPS, ON_CALL)
    declare_everything(bucket, settings.stages, ["ops", "on-call"])

    run_once(
        settings=settings,
        store=AlertStore(bucket, BASE),
        dagster={"dev": FakeDagster()},
        notifiers={"ops": ops, "on-call": on_call},
        now=NOW,
    )

    assert on_call.sent == []


class _Response:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code, self._body = status, body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _reader(tmp_path: Path, get: Any) -> StageHealthReader:
    token = tmp_path / "token"
    token.write_text("projected-token\n")
    return stage_health_reader("http://platform-api.olf-system:8080/", str(token), get=get)


def test_the_reader_sends_the_projected_token_and_parses_services(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def get(url: str, **kwargs: Any) -> _Response:
        seen.update(url=url, **kwargs)
        return _Response(
            200,
            {"stage": "dev", "services": [{"component": "trino.workloads", "state": "critical", "reason": "x"}]},
        )

    services = _reader(tmp_path, get)("dev")

    assert services == [ServiceState("trino.workloads", HealthState.CRITICAL, "x")]
    assert seen["url"] == "http://platform-api.olf-system:8080/api/v1/stages/dev/health"
    assert seen["headers"]["Authorization"] == "Bearer projected-token"
    assert seen["timeout"] > 0


@pytest.mark.parametrize(
    "answer",
    [
        _Response(503),
        _Response(401),
        _Response(403),
        _Response(200, ValueError("not json")),
        _Response(200, {"stage": "prod", "services": []}),
        _Response(200, {"stage": "dev"}),
        _Response(200, {"stage": "dev", "services": [{"component": "x", "state": "great"}]}),
        requests.Timeout("slow"),
    ],
    ids=["5xx", "401", "403", "not-json", "wrong-stage", "no-services", "bad-state", "timeout"],
)
def test_the_reader_raises_on_anything_but_a_clean_answer(tmp_path: Path, answer: Any) -> None:
    def get(url: str, **kwargs: Any) -> _Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(HealthReadError) as raised:
        _reader(tmp_path, get)("dev")
    assert "projected-token" not in str(raised.value)


def test_a_missing_token_file_is_unreadable_not_healthy(tmp_path: Path) -> None:
    read = stage_health_reader("http://x:8080", str(tmp_path / "absent"), get=lambda *a, **k: _Response(200, {}))

    with pytest.raises(HealthReadError):
        read("dev")
