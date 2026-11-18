"""The metrics routes against a loopback Prometheus and Dagster: every state, and never a zero for a missing answer."""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import Mint
from fastapi.testclient import TestClient
from test_reads import UNREACHABLE, FakeDagster, _as, _Handler, _Server, _settings

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings, SettingsError

CPU = "/api/v1/stages/dev/metrics/cpu_usage_cores?range=1h"
CATALOG = "/api/v1/stages/dev/metrics"


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


@pytest.fixture
def dagster() -> Iterator[FakeDagster]:
    fake = FakeDagster()
    yield fake
    fake.close()


def _api(settings: Settings, **metrics: Any) -> TestClient:
    return TestClient(create_app(_settings(settings, **metrics)))


@pytest.fixture
def api(settings: Settings, prometheus: FakePrometheus, dagster: FakeDagster) -> TestClient:
    return _api(
        settings,
        metrics_enabled=True,
        metrics_url=prometheus.url,
        metrics_retention_days=7,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
    )


def _steps(seconds: int, count: int) -> list[int]:
    end = int(time.time()) // seconds * seconds
    return [end - seconds * (count - 1 - index) for index in range(count)]


def test_a_series_answers_one_point_per_step_and_a_step_without_a_sample_is_null(
    api: TestClient, mint: Mint, prometheus: FakePrometheus
) -> None:
    steps = _steps(60, 60)
    prometheus.matrix = [
        {
            "metric": {"stage": "dev", "component": "trino", "pod": "trino-coordinator-0"},
            "values": [[steps[0], "0.5"], [steps[-1], "0"]],
        }
    ]

    body = api.get(CPU, headers=_as(mint, "data-engineer")).json()

    assert {key: body[key] for key in ("series", "stage", "source", "state", "range", "step_seconds", "unit")} == {
        "series": "cpu_usage_cores",
        "stage": "dev",
        "source": "prometheus",
        "state": "available",
        "range": "1h",
        "step_seconds": 60,
        "unit": "cores",
    }
    assert body["complete"] is True and body["covered_from"] is None
    (line,) = body["lines"]
    # A label outside the catalog's, such as the pod (which can carry a run ID), is never served.
    assert line["labels"] == {"stage": "dev", "component": "trino"}
    values = [value for _, value in line["points"]]
    assert len(values) == 60
    assert values[0] == 0.5 and values[-1] == 0 and values[1:-1] == [None] * 58


def test_the_query_names_only_the_series_rule_the_stage_and_the_catalogs_step(
    api: TestClient, mint: Mint, prometheus: FakePrometheus
) -> None:
    prometheus.up = [{"metric": {"job": "trino", "stage": "dev"}, "value": [0, "1"]}]

    api.get("/api/v1/stages/dev/metrics/trino_failed_queries?range=24h", headers=_as(mint, "data-engineer"))

    targets, points = prometheus.queries
    assert targets["query"] == 'max_over_time(up{job=~"trino",stage="dev"}[86400s])'
    assert points["query"] == 'increase(olf:trino_failed_queries{stage="dev"}[300s])'
    assert points["step"] == "300.0"
    assert float(points["end"]) - float(points["start"]) == 86400 - 300


def test_prometheus_down_is_unavailable_with_no_lines(api: TestClient, mint: Mint, prometheus: FakePrometheus) -> None:
    prometheus.fail_status = 503

    body = api.get(CPU, headers=_as(mint, "data-engineer")).json()

    assert (body["state"], body["reason"], body["lines"]) == ("unavailable", "the metrics backend did not answer", [])


def test_prometheus_unreachable_is_unavailable(settings: Settings, mint: Mint) -> None:
    api = _api(settings, metrics_enabled=True, metrics_url=UNREACHABLE)

    body = api.get(CPU, headers=_as(mint, "data-engineer")).json()

    assert body["state"] == "unavailable" and body["lines"] == []


def test_targets_down_for_the_whole_range_are_unavailable_even_with_old_samples(
    api: TestClient, mint: Mint, prometheus: FakePrometheus
) -> None:
    prometheus.up = [{"metric": {"job": "kubelet-resource"}, "value": [0, "0"]}]
    prometheus.matrix = [{"metric": {"stage": "dev"}, "values": [[_steps(60, 1)[0], "0"]]}]

    body = api.get(CPU, headers=_as(mint, "data-engineer")).json()

    assert body["state"] == "unavailable" and body["lines"] == []
    assert body["reason"] == "every target behind the series was down for the whole range"


def test_a_series_with_no_target_or_no_samples_is_unavailable_never_zero(
    api: TestClient, mint: Mint, prometheus: FakePrometheus
) -> None:
    prometheus.up = []
    assert api.get(CPU, headers=_as(mint, "data-engineer")).json()["reason"] == "no target is scraped"

    prometheus.up = [{"metric": {"job": "kubelet-resource"}, "value": [0, "1"]}]
    prometheus.matrix = []
    body = api.get(CPU, headers=_as(mint, "data-engineer")).json()
    assert body["state"] == "unavailable" and body["lines"] == []


def test_a_range_past_retention_is_refused_not_answered_short(settings: Settings, mint: Mint) -> None:
    api = _api(settings, metrics_enabled=True, metrics_url=UNREACHABLE, metrics_retention_days=3)

    answer = api.get("/api/v1/stages/dev/metrics/cpu_usage_cores?range=7d", headers=_as(mint, "data-engineer"))

    assert answer.status_code == 400
    assert answer.json()["detail"] == "range 7d is longer than the metrics retention of 3 days"


@pytest.mark.parametrize("path", [CPU.split("?")[0], CPU.replace("1h", "30d")])
def test_only_a_catalog_range_is_accepted(api: TestClient, mint: Mint, prometheus: FakePrometheus, path: str) -> None:
    assert api.get(path, headers=_as(mint, "data-engineer")).status_code == 422
    assert prometheus.queries == []


def test_a_query_parameter_never_reaches_prometheus(api: TestClient, mint: Mint, prometheus: FakePrometheus) -> None:
    api.get(CPU + "&query=up&step=1", headers=_as(mint, "data-engineer"))

    assert [(sent["query"], sent.get("step")) for sent in prometheus.queries] == [
        ('max_over_time(up{job=~"kubelet-resource"}[3600s])', None),
        ('olf:cpu_usage_cores{stage="dev"}', "60.0"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/stages/dev/metrics/up?range=1h",
        "/api/v1/stages/dev/metrics/node_cpu_usage_cores?range=1h",
        "/api/v1/platform/metrics/trino_failed_queries?range=1h",
        "/api/v1/platform/metrics/pipeline_runs?range=1h",
    ],
)
def test_a_series_outside_the_catalog_or_its_scope_is_not_found(api: TestClient, mint: Mint, path: str) -> None:
    assert api.get(path, headers=_as(mint, "data-engineer")).status_code == 404


@pytest.mark.parametrize(
    ("metrics", "state"),
    [
        ({}, "disabled"),
        ({"metrics_enabled": True}, "not_provided"),
    ],
)
def test_no_backend_answers_disabled_or_not_provided_and_asks_nothing(
    settings: Settings, mint: Mint, metrics: dict[str, Any], state: str
) -> None:
    api = _api(settings, **metrics)

    body = api.get(CPU, headers=_as(mint, "data-engineer")).json()
    platform = api.get("/api/v1/platform/metrics/node_cpu_usage_cores?range=1h", headers=_as(mint, "data-engineer"))

    assert (body["state"], body["lines"]) == (state, [])
    assert platform.json()["state"] == state and platform.json()["stage"] is None


def test_a_platform_series_reads_the_platform_stage(api: TestClient, mint: Mint, prometheus: FakePrometheus) -> None:
    prometheus.matrix = [{"metric": {"stage": "platform", "component": "prometheus"}, "values": []}]

    body = api.get("/api/v1/platform/metrics/cpu_usage_cores?range=6h", headers=_as(mint, "data-engineer")).json()

    assert body["stage"] is None and body["state"] == "available"
    assert prometheus.queries[-1]["query"] == 'olf:cpu_usage_cores{stage="platform"}'


def test_pipeline_runs_come_from_dagster_with_their_status_label(
    settings: Settings, mint: Mint, dagster: FakeDagster
) -> None:
    created = _steps(60, 2)[0] + 1
    dagster.runs = [
        {
            "runId": "r1",
            "jobName": "orders_job",
            "status": "FAILURE",
            "creationTime": created,
            "startTime": created,
            "endTime": created + 30,
            "tags": [],
        }
    ]
    # No metrics backend at all, as on Slim: the Dagster series still answer.
    api = _api(settings, dagster_graphql_urls={"dev": f"{dagster.url}/graphql"})

    body = api.get("/api/v1/stages/dev/metrics/pipeline_runs?range=1h", headers=_as(mint, "data-engineer")).json()

    assert (body["source"], body["state"], body["complete"]) == ("dagster", "available", True)
    failed = next(line for line in body["lines"] if line["labels"]["status"] == "failed")
    assert failed["labels"] == {"stage": "dev", "job": "orders_job", "status": "failed"}
    assert sum(value for _, value in failed["points"]) == 1


def test_pipeline_series_read_unavailable_when_dagster_is_down(settings: Settings, mint: Mint) -> None:
    api = _api(settings, dagster_graphql_urls={"dev": UNREACHABLE})

    body = api.get(
        "/api/v1/stages/dev/metrics/pipeline_run_duration_seconds?range=7d", headers=_as(mint, "data-engineer")
    ).json()

    assert (body["state"], body["lines"]) == ("unavailable", [])


def test_the_catalog_lists_each_stage_series_with_its_state_and_ranges(
    settings: Settings, mint: Mint, prometheus: FakePrometheus, dagster: FakeDagster
) -> None:
    prometheus.up = [
        {"metric": {"job": "kubelet-resource"}, "value": [0, "1"]},
        {"metric": {"job": "trino", "stage": "dev"}, "value": [0, "0"]},
        {"metric": {"job": "trino", "stage": "prod"}, "value": [0, "1"]},
    ]
    api = _api(
        settings,
        metrics_enabled=True,
        metrics_url=prometheus.url,
        metrics_retention_days=2,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
    )

    body = api.get(CATALOG, headers=_as(mint, "data-engineer")).json()

    series = {item["name"]: item for item in body["series"]}
    assert "node_cpu_usage_cores" not in series
    assert series["cpu_usage_cores"]["state"] == "available"
    assert series["cpu_usage_cores"]["ranges"] == ["1h", "6h", "24h"]
    # PROD's coordinator being up says nothing about DEV's.
    assert series["trino_running_queries"]["state"] == "unavailable"
    assert series["container_restarts"]["reason"] == "no target is scraped"
    assert series["pipeline_runs"]["labels"] == ["stage", "job", "status"]
    assert (series["pipeline_runs"]["state"], series["pipeline_runs"]["ranges"][-1]) == ("available", "7d")


def test_the_catalog_never_reads_available_without_a_backend_or_a_dagster(settings: Settings, mint: Mint) -> None:
    body = _api(settings).get(CATALOG, headers=_as(mint, "data-engineer")).json()

    assert {item["state"] for item in body["series"] if item["source"] == "prometheus"} == {"disabled"}
    assert {item["state"] for item in body["series"] if item["source"] == "dagster"} == {"disabled"}


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


@pytest.mark.parametrize("path", [CATALOG, CPU, "/api/v1/platform/metrics/node_cpu_usage_cores?range=1h"])
def test_an_analyst_reads_no_metrics(api: TestClient, mint: Mint, path: str) -> None:
    assert api.get(path, headers=_as(mint, "analyst")).status_code == 403


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
