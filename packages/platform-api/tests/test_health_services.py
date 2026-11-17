"""The stage health route with service probes: rollup, the event loop staying free, and a probe that cannot look."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
import pytest
from conftest import Mint
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openlakeforge_core import HealthState, Scope, Signal, service_signal
from test_health import STAGE, ScheduledDagster, _run, _schedule
from test_reads import UNREACHABLE, _as, _settings

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.service_probes import StageTargets
from openlakeforge_platform_api.settings import Settings, SettingsError

HOUR = 3600
NOW = datetime.now(UTC)


def _signal(component: str, state: HealthState) -> Signal:
    scope = Scope(stage="dev")
    return service_signal(
        scope, component, state, f"{component} is {state.value}", now=NOW, max_age=timedelta(minutes=1)
    )


class FakeProbes:
    def __init__(self, signals: list[Signal] | None = None, *, fail: bool = False) -> None:
        self.signals = signals or []
        self.fail = fail
        self.targets: list[StageTargets] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.thread: int | None = None

    def probe(self, targets: StageTargets) -> list[Signal]:
        self.targets.append(targets)
        self.thread = threading.get_ident()
        self.entered.set()
        self.release.wait(timeout=10)
        if self.fail:
            raise RuntimeError("the probe broke: secret-detail")
        return self.signals


@pytest.fixture
def dagster() -> Iterator[ScheduledDagster]:
    fake = ScheduledDagster()
    fake.runs = [_run("r1", "SUCCESS", HOUR)]
    fake.schedules = [_schedule("STOPPED", 2 * HOUR)]
    yield fake
    fake.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _app(settings: Settings, dagster: ScheduledDagster, probes: FakeProbes) -> FastAPI:
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
        stage_namespaces={"dev": "olf-dev"},
        trino_urls={"dev": "http://trino.olf-dev:8080"},
        file_buckets={"dev": ["dev-bronze", "dev-silver"]},
    )
    return create_app(configured, service_probes=probes)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([HealthState.HEALTHY, HealthState.HEALTHY], "healthy"),
        ([HealthState.HEALTHY, HealthState.WARNING], "warning"),
        ([HealthState.HEALTHY, HealthState.UNKNOWN], "unknown"),
        ([HealthState.HEALTHY, HealthState.DISABLED], "healthy"),
        ([HealthState.WARNING, HealthState.UNKNOWN, HealthState.DISABLED], "warning"),
        ([HealthState.CRITICAL, HealthState.HEALTHY], "critical"),
    ],
)
def test_service_signals_roll_into_the_stage_state(
    settings: Settings, dagster: ScheduledDagster, mint: Mint, states: list[HealthState], expected: str
) -> None:
    probes = FakeProbes([_signal(f"component-{i}", state) for i, state in enumerate(states)])
    api = TestClient(_app(settings, dagster, probes))

    body = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    assert body["state"] == expected
    assert [(item["component"], item["state"]) for item in body["services"]] == [
        (f"component-{i}", state.value) for i, state in enumerate(states)
    ]
    assert {item["name"]: item["status"] for item in body["integrations"]}["services"] == "available"


def test_each_service_row_names_its_source_and_how_long_its_answer_is_usable(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    probes = FakeProbes([_signal("trino.readiness", HealthState.HEALTHY), _signal("component-x", HealthState.HEALTHY)])
    api = TestClient(_app(settings, dagster, probes))

    body = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    rows = {item["component"]: item for item in body["services"]}
    assert rows["trino.readiness"]["source"] == "trino-http"
    assert rows["component-x"]["source"] == "platform-api"
    assert rows["trino.readiness"]["max_age_seconds"] == 60


def test_the_probes_are_aimed_at_the_stages_own_namespace_and_endpoints(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    probes = FakeProbes()
    api = TestClient(_app(settings, dagster, probes))

    api.get(STAGE, headers=_as(mint, "data-engineer"))

    assert probes.targets == [
        StageTargets(
            stage="dev",
            namespace="olf-dev",
            dagster_graphql_url=f"{dagster.url}/graphql",
            trino_url="http://trino.olf-dev:8080",
            bucket="dev-bronze",
        )
    ]


def test_the_stages_archive_and_the_collector_probe_reach_the_probes(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    probes = FakeProbes()
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
        stage_namespaces={"dev": "olf-dev"},
        log_archives={"dev": {"base_uri": "s3://ops/activations/dev/logs", "namespace": "olf-dev"}},
        log_collector_probe_url="http://log-collector-probe.olf-system.svc:2021",
        polaris_health_url="http://polaris-mgmt.olf-system.svc:8182",
    )
    api = TestClient(create_app(configured, service_probes=probes))

    api.get(STAGE, headers=_as(mint, "data-engineer"))

    target = probes.targets[0]
    assert target.log_archive_base_uri == "s3://ops/activations/dev/logs"
    assert target.log_archive_namespace == "olf-dev"
    assert target.spool_probe_url == "http://log-collector-probe.olf-system.svc:2021"
    assert target.polaris_health_url == "http://polaris-mgmt.olf-system.svc:8182"


def test_a_probe_that_raises_is_unknown_and_leaks_nothing(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    api = TestClient(_app(settings, dagster, FakeProbes(fail=True)))

    response = api.get(STAGE, headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert response.json()["state"] == "unknown"
    assert [item["state"] for item in response.json()["services"]] == ["unknown"]
    assert "secret-detail" not in response.text


def test_an_unreadable_dagster_is_unknown_even_when_every_service_probe_is_healthy(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    probes = FakeProbes(
        [_signal("trino.readiness", HealthState.HEALTHY), _signal("dagster.workloads", HealthState.HEALTHY)]
    )
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{UNREACHABLE}/graphql"},
        stage_namespaces={"dev": "olf-dev"},
    )
    api = TestClient(create_app(configured, service_probes=probes))

    body = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    assert body["state"] == "unknown"
    assert {item["name"]: item["status"] for item in body["integrations"]}["dagster"] == "unavailable"


def test_a_stage_with_no_namespace_is_not_probed(settings: Settings, dagster: ScheduledDagster, mint: Mint) -> None:
    probes = FakeProbes([_signal("trino.readiness", HealthState.CRITICAL)])
    configured = _settings(settings, dagster_graphql_urls={"dev": f"{dagster.url}/graphql"})
    api = TestClient(create_app(configured, service_probes=probes))

    body = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    assert probes.targets == []
    assert body["services"] == []
    assert body["state"] == "healthy"
    assert {item["name"]: item["status"] for item in body["integrations"]}["services"] == "disabled"


def test_the_real_probes_outside_a_cluster_are_unknown_never_healthy(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
        stage_namespaces={"dev": "olf-dev"},
        trino_urls={"dev": UNREACHABLE},
    )
    with TestClient(create_app(configured)) as api:
        body = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    by_component = {item["component"]: item["state"] for item in body["services"]}
    assert by_component["trino.readiness"] == "unknown"
    assert by_component["trino.workloads"] == "unknown"
    assert by_component["dagster.workloads"] == "unknown"
    assert body["state"] == "unknown"


@pytest.mark.anyio
async def test_a_slow_probe_does_not_block_the_event_loop(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    probes = FakeProbes([_signal("trino.readiness", HealthState.HEALTHY)])
    probes.release.clear()
    app = _app(settings, dagster, probes)
    headers = _as(mint, "data-engineer")
    loop_thread = threading.get_ident()
    result: dict[str, httpx.Response] = {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:

        async def health() -> None:
            result["health"] = await client.get(STAGE, headers=headers)

        async with anyio.create_task_group() as group:
            group.start_soon(health)
            with anyio.fail_after(5):
                await anyio.to_thread.run_sync(probes.entered.wait)
            # The probe is blocked mid-call: a loop that ran it would be unable to serve this request.
            with anyio.fail_after(5):
                alive = await client.get("/api/healthz")
            probes.release.set()

    assert alive.status_code == 200
    assert probes.thread != loop_thread
    assert result["health"].status_code == 200


def test_settings_name_each_stage_namespace_and_trino_base_url(settings: Settings) -> None:
    url = "http://trino.olf-dev.svc.cluster.local:8080"
    configured = _settings(settings, stage_namespaces={"dev": "olf-dev"}, trino_urls={"dev": url})

    assert configured.stage_namespaces == {"dev": "olf-dev"}
    assert configured.trino_urls == {"dev": url}
    unset = _settings(settings)
    assert unset.stage_namespaces == {} and unset.trino_urls == {}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"stage_namespaces": {"qa": "olf-qa"}}, "stage_namespaces names stages that are not enabled: qa"),
        ({"stage_namespaces": {"dev": ""}}, "must name a namespace"),
        ({"trino_urls": {"qa": "http://x:8080"}}, "trino_urls names stages that are not enabled: qa"),
        ({"trino_urls": {"dev": "trino:8080"}}, "http.s. base URL"),
        ({"log_collector_probe_url": "https://probe:2021"}, "log_collector_probe_url"),
        ({"log_collector_probe_url": "http://probe"}, "log_collector_probe_url"),
        ({"polaris_health_url": "polaris-mgmt:8182"}, "polaris_health_url"),
    ],
)
def test_settings_refuse_probe_targets_that_cannot_be_probed(
    settings: Settings, extra: dict[str, Any], message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        _settings(settings, **extra)


def test_the_services_route_answers_from_the_probes_alone_and_never_reads_dagster(
    settings: Settings, dagster: ScheduledDagster, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the services route must not read Dagster")

    monkeypatch.setattr("openlakeforge_platform_api.health_routes._read", hang)
    monkeypatch.setattr("openlakeforge_platform_api.health_routes.read_stage_pipelines", hang)
    probes = FakeProbes([_signal("trino.workloads", HealthState.CRITICAL)])
    api = TestClient(_app(settings, dagster, probes))

    response = api.get(f"{STAGE}/services", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"stage", "services"}
    assert body["stage"] == "dev"
    assert [(s["component"], s["state"]) for s in body["services"]] == [("trino.workloads", "critical")]
    assert api.get(f"{STAGE}/services", headers=_as(mint, "viewer")).status_code == 403
