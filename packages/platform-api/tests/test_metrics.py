"""The metrics settings and the health routes' `metrics` integration against a loopback Prometheus."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import Mint
from fastapi.testclient import TestClient
from test_reads import UNREACHABLE, _as, _Handler, _Server, _settings

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings, SettingsError


class FakePrometheus(_Server):
    """Answers instant and range queries as Prometheus 3 shapes them, recording every query it was sent."""

    def __init__(self) -> None:
        self.up: list[dict[str, Any]] = [{"metric": {"job": "kubelet-resource"}, "value": [0, "1"]}]
        self.matrix: list[dict[str, Any]] = []
        self.fail_status: int | None = None
        self.queries: list[dict[str, str]] = []
        server = self

        class Handler(_Handler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's handler name
                parts = urllib.parse.urlsplit(self.path)
                params = dict(urllib.parse.parse_qsl(parts.query))
                server.queries.append({"path": parts.path, **params})
                if server.fail_status:
                    return self.reply(server.fail_status, {"status": "error", "error": "down"})
                if parts.path == "/api/v1/query_range":
                    return self.reply(
                        200, {"status": "success", "data": {"resultType": "matrix", "result": server.matrix}}
                    )
                result = [] if params["query"] == "vector(1)" else server.up
                self.reply(200, {"status": "success", "data": {"resultType": "vector", "result": result}})

        super().__init__(Handler)


@pytest.fixture
def prometheus() -> Iterator[FakePrometheus]:
    fake = FakePrometheus()
    yield fake
    fake.close()


def _api(settings: Settings, **metrics: Any) -> TestClient:
    return TestClient(create_app(_settings(settings, **metrics)))


@pytest.mark.parametrize(
    ("metrics", "status"),
    [
        ({}, "disabled"),
        ({"metrics_enabled": True}, "not_provided"),
        ({"metrics_enabled": True, "metrics_url": UNREACHABLE}, "unavailable"),
    ],
)
def test_the_health_routes_report_the_metrics_integration_without_folding_it_into_the_state(
    settings: Settings, mint: Mint, metrics: dict[str, Any], status: str
) -> None:
    api = _api(settings, **metrics)

    stage = api.get("/api/v1/stages/dev/health", headers=_as(mint, "data-engineer")).json()

    assert {item["name"]: item["status"] for item in stage["integrations"]}["metrics"] == status
    assert stage["state"] == "unknown"


def test_the_metrics_integration_is_available_when_the_backend_answers(
    settings: Settings, mint: Mint, prometheus: FakePrometheus
) -> None:
    api = _api(settings, metrics_enabled=True, metrics_url=prometheus.url)

    stage = api.get("/api/v1/stages/dev/health", headers=_as(mint, "data-engineer")).json()

    assert {item["name"]: item["status"] for item in stage["integrations"]}["metrics"] == "available"


@pytest.mark.parametrize(
    ("metrics", "message"),
    [
        ({"metrics_enabled": True, "metrics_url": "prometheus:9090"}, "metrics_url must be an http"),
        ({"metrics_url": "http://prometheus:9090"}, "metrics_enabled is not"),
        ({"metrics_retention_days": 0}, "metrics_retention_days must be positive"),
    ],
)
def test_contradictory_metrics_settings_refuse_the_start(
    settings: Settings, metrics: dict[str, Any], message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        _settings(settings, **metrics)
