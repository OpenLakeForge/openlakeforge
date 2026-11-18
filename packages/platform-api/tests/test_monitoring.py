"""The monitoring capabilities document: permitted stages from the shipped matrix, sources from configuration alone."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.monitoring import MONITORING_OPERATIONS
from openlakeforge_platform_api.settings import Settings

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
    )


def _api(settings: Settings, **kwargs: Any) -> TestClient:
    kwargs.setdefault("service_probes", RefusingProbes())
    kwargs.setdefault("object_store", RefusingStore())
    kwargs.setdefault("quality_readers", {"dev": RefusingStore()})
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
        "alerts": "not_provided",
    }
    assert _sources(body, "prod") == {
        "dagster": "available",
        "services": "available",
        "quality": "not_provided",
        "quality-sync": "not_provided",
        "logs": "not_provided",
        "metrics": "available",
        "alerts": "not_provided",
    }


def test_a_stage_with_no_source_is_never_available_and_says_why(settings: Settings, mint: Mint) -> None:
    body = _api(_configured(settings)).get(PATH, headers=_as(mint, "platform-admin")).json()
    uat = next(entry for entry in body["stages"] if entry["stage"] == "uat")
    by_name = {capability["name"]: capability for capability in uat["capabilities"]}

    assert {name: entry["status"] for name, entry in by_name.items() if name != "metrics"} == {
        "dagster": "disabled",
        "services": "disabled",
        "quality": "not_provided",
        "quality-sync": "not_provided",
        "logs": "not_provided",
        "alerts": "not_provided",
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
