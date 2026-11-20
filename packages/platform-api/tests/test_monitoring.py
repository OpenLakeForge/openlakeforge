"""The monitoring capabilities document: permitted stages from the shipped matrix, sources from configuration alone."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.monitoring import MONITORING_OPERATIONS
from openlakeforge_platform_api.settings import Settings, SettingsError

PATH = "/api/v1/monitoring-capabilities"
STAGES = ["dev", "uat", "prod"]
EVERY_OPERATION = list(MONITORING_OPERATIONS)
WITHOUT_LOGS = [name for name in MONITORING_OPERATIONS if name != "list_logs"]
SECRET_LOOKING = ("http", "s3://", "olf-", "dagster-dev", "lakehouse-dev")


class RefusingProbes:
    """A prober that fails the test if the route reads through it: capabilities are configuration only."""

    def probe(self, targets: Any) -> Any:
        raise AssertionError("the capabilities route must not probe")


class RefusingStore:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the capabilities route must not call the object store ({name})")


def _settings(settings: Settings, **extra: Any) -> Settings:
    return Settings.from_mapping(
        {
            "issuer_url": settings.issuer_url,
            "audiences": list(settings.audiences),
            "role_claim": settings.role_claim,
            "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
            "precedence": list(settings.precedence),
            "stages": STAGES,
            **extra,
        }
    )


def _configured(settings: Settings) -> Settings:
    """DEV with every source, PROD with some, UAT with none."""
    return _settings(
        settings,
        dagster_graphql_urls={"dev": "http://dagster-dev:80/graphql", "prod": "http://dagster-prod:80/graphql"},
        stage_namespaces={"dev": "lakehouse-dev", "prod": "lakehouse-prod"},
        log_archives={"dev": {"base_uri": "s3://olf-ops/logs/dev/", "namespace": "lakehouse-dev"}},
        quality_manifests={"dev": "s3://olf-ops/activations/dev/"},
        quality_sync_summaries={"dev": "s3://olf-ops/activations/dev/quality-sync/summary.json"},
        metrics_enabled=True,
        metrics_url="http://prometheus:9090",
        alerts_state_base_uri="s3://olf-ops/alerts/state/",
    )


def _api(settings: Settings, **kwargs: Any) -> TestClient:
    kwargs.setdefault("service_probes", RefusingProbes())
    kwargs.setdefault("object_store", RefusingStore())
    kwargs.setdefault("quality_readers", {"dev": RefusingStore()})
    kwargs.setdefault("alert_state_store", RefusingStore())
    return TestClient(create_app(settings, **kwargs))


def _as(mint: Mint, role: str) -> dict[str, str]:
    return bearer(mint(groups=list(ROLE_MAPPING[role])))


def _sources(body: dict[str, Any], stage: str) -> dict[str, str]:
    entry = next(item for item in body["stages"] if item["stage"] == stage)
    return {capability["name"]: capability["status"] for capability in entry["capabilities"]}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("platform-admin", {"dev": EVERY_OPERATION, "uat": EVERY_OPERATION, "prod": EVERY_OPERATION}),
        ("data-engineer", {"dev": EVERY_OPERATION, "uat": WITHOUT_LOGS, "prod": WITHOUT_LOGS}),
        ("analyst", {}),
        ("viewer", {}),
    ],
)
def test_stages_and_operations_follow_the_shipped_matrix(
    settings: Settings, mint: Mint, role: str, expected: dict[str, list[str]]
) -> None:
    response = _api(_configured(settings)).get(PATH, headers=_as(mint, role))

    assert response.status_code == 200
    assert {entry["stage"]: entry["operations"] for entry in response.json()["stages"]} == expected


@pytest.mark.parametrize("role", ["platform-admin", "data-engineer", "analyst", "viewer"])
def test_an_operation_is_offered_exactly_where_its_route_admits_the_caller(
    settings: Settings, mint: Mint, role: str
) -> None:
    # No source is configured, so an admitted call ends in a 404, 502 or 503 of its own: only the matrix answers 403.
    app = create_app(_settings(settings), quality_readers={})
    api = TestClient(app)
    paths = {route.name: route.path for route in app.routes if isinstance(route, APIRoute)}
    body = api.get(PATH, headers=_as(mint, role)).json()
    offered = {entry["stage"]: set(entry["operations"]) for entry in body["stages"]}

    for stage in STAGES:
        for name in MONITORING_OPERATIONS:
            path = paths[name].format(stage=stage, run_id="run-1", series="cpu")
            admitted = api.get(path, headers=_as(mint, role)).status_code != 403
            assert admitted == (name in offered.get(stage, set())), (role, stage, name)


def test_the_failed_run_reads_are_offered_where_a_run_may_be_opened(settings: Settings, mint: Mint) -> None:
    # The Portal's Failed-run screen (#1324) learns from this answer whether it may open a run and its failure.
    body = _api(_configured(settings)).get(PATH, headers=_as(mint, "data-engineer")).json()

    assert [entry["stage"] for entry in body["stages"]] == STAGES
    for entry in body["stages"]:
        assert {"get_run", "get_run_failure"} <= set(entry["operations"]), entry["stage"]


def test_only_enabled_stages_are_listed_in_their_order(settings: Settings, mint: Mint) -> None:
    body = _api(_settings(settings, stages=["prod", "dev"])).get(PATH, headers=_as(mint, "platform-admin")).json()

    assert [entry["stage"] for entry in body["stages"]] == ["prod", "dev"]


def test_sources_are_reported_from_configuration(settings: Settings, mint: Mint) -> None:
    body = _api(_configured(settings)).get(PATH, headers=_as(mint, "platform-admin")).json()

    assert _sources(body, "dev") == {
        "dagster": "available",
        "services": "available",
        "quality": "available",
        "quality-sync": "available",
        "logs": "available",
        "metrics": "available",
        "alerts": "available",
    }
    assert _sources(body, "prod") == {
        "dagster": "available",
        "services": "available",
        "quality": "not_provided",
        "quality-sync": "not_provided",
        "logs": "not_provided",
        "metrics": "available",
        "alerts": "available",
    }


def test_a_stage_with_no_source_is_never_available_and_says_why(settings: Settings, mint: Mint) -> None:
    body = _api(_configured(settings)).get(PATH, headers=_as(mint, "platform-admin")).json()
    uat = next(entry for entry in body["stages"] if entry["stage"] == "uat")
    by_name = {capability["name"]: capability for capability in uat["capabilities"]}

    # Metrics and alerts are platform-wide, so they read the same on every stage.
    assert {name: entry["status"] for name, entry in by_name.items() if name not in ("metrics", "alerts")} == {
        "dagster": "disabled",
        "services": "disabled",
        "quality": "not_provided",
        "quality-sync": "not_provided",
        "logs": "not_provided",
    }
    assert all(entry["reason"] for entry in by_name.values())


def test_no_host_uri_or_bucket_appears_in_the_answer(settings: Settings, mint: Mint) -> None:
    text = _api(_configured(settings)).get(PATH, headers=_as(mint, "platform-admin")).text

    for fragment in SECRET_LOOKING:
        assert fragment not in text


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ({}, "disabled"),
        ({"metrics_enabled": True}, "not_provided"),
        ({"metrics_enabled": True, "metrics_url": "http://prometheus:9090"}, "available"),
    ],
)
def test_metrics_tell_a_profile_that_turns_them_off_from_a_provider_without_a_backend(
    settings: Settings, mint: Mint, extra: dict[str, Any], status: str
) -> None:
    body = _api(_settings(settings, **extra)).get(PATH, headers=_as(mint, "platform-admin")).json()

    assert {_sources(body, stage)["metrics"] for stage in STAGES} == {status}


def test_an_archive_without_a_key_to_read_it_is_unavailable(
    settings: Settings, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _configured(settings)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    api = TestClient(create_app(configured, service_probes=RefusingProbes(), quality_readers={}))

    body = api.get(PATH, headers=_as(mint, "platform-admin")).json()

    assert _sources(body, "dev")["logs"] == "unavailable"


def test_without_probes_services_are_disabled_and_publication_unavailable(settings: Settings, mint: Mint) -> None:
    # No stage namespace, so the app builds no probes, as on a deployment that renders none.
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": "http://dagster-dev:80/graphql"},
        quality_sync_summaries={"dev": "s3://olf-ops/activations/dev/quality-sync/summary.json"},
    )
    api = TestClient(create_app(configured, object_store=RefusingStore(), quality_readers={}))

    sources = _sources(api.get(PATH, headers=_as(mint, "platform-admin")).json(), "dev")

    assert sources["services"] == "disabled"
    assert sources["quality-sync"] == "unavailable"


def test_a_token_is_still_required(settings: Settings) -> None:
    assert _api(_configured(settings)).get(PATH).status_code == 401


@pytest.mark.parametrize(
    "extra",
    [{"alert_state_store": None}, {}],
    ids=["no-key", "no-state-uri"],
)
def test_alerts_are_not_provided_without_a_state_uri_or_a_key(
    settings: Settings, mint: Mint, extra: dict[str, Any]
) -> None:
    configured = _configured(settings)
    if not extra:
        configured = _settings(settings, dagster_graphql_urls={"dev": "http://dagster-dev:80/graphql"})
    api = _api(configured, alert_state_store=None if extra else RefusingStore())

    body = api.get(PATH, headers=_as(mint, "platform-admin")).json()

    assert _sources(body, "dev")["alerts"] == "not_provided"


def _retention(body: dict[str, Any]) -> dict[str, int | None]:
    return {entry["stage"]: entry["log_retention_days"] for entry in body["stages"]}


def test_the_log_retention_is_stated_on_each_stage_with_an_archive(settings: Settings, mint: Mint) -> None:
    configured = _settings(
        settings,
        log_archives={
            "dev": {"base_uri": "s3://olf-ops/logs/dev/", "namespace": "lakehouse-dev"},
            "prod": {"base_uri": "s3://olf-ops/logs/prod/", "namespace": "lakehouse-prod"},
        },
        log_archive_retention_days=30,
    )

    body = _api(configured).get(PATH, headers=_as(mint, "platform-admin")).json()

    assert _retention(body) == {"dev": 30, "uat": None, "prod": 30}


def test_a_provider_that_applies_no_retention_states_none(settings: Settings, mint: Mint) -> None:
    body = _api(_configured(settings)).get(PATH, headers=_as(mint, "platform-admin")).json()

    assert _retention(body) == {"dev": None, "uat": None, "prod": None}


def test_a_retention_that_is_not_positive_refuses_the_start(settings: Settings) -> None:
    with pytest.raises(SettingsError, match="log_archive_retention_days must be positive"):
        _settings(settings, log_archive_retention_days=0)
