"""POST /api/v1/stages/{stage}/runs: who may launch, where, and the audit record each request leaves."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient
from test_reads import UNREACHABLE, FakeDagster

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.audit import AuditLog
from openlakeforge_platform_api.settings import Settings

AUDIT_BASE = "s3://ops/audit/platform-api/"
NOW = datetime(2026, 10, 31, 12, 30, 5, 123456, tzinfo=UTC)


class LaunchingDagster(FakeDagster):
    """The read fake, plus launchRun: each launch is recorded with its tags and answered with a new run id."""

    def __init__(self) -> None:
        super().__init__()
        self.launches: list[dict[str, Any]] = []

    def answer(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "mutation LaunchRun" in query:
            self.launches.append(variables["executionParams"])
            return {"launchRun": {"__typename": "LaunchRunSuccess", "run": {"runId": "run-new", "status": "QUEUED"}}}
        return super().answer(query, variables)


class AuditStore:
    """put_object into a dict, as boto3 answers it; `fail_after` lets that many writes succeed, then refuses."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.fail_after: int | None = None

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict[str, Any]:  # noqa: N803
        if self.fail_after is not None and len(self.objects) >= self.fail_after:
            raise ConnectionError("seaweedfs.olf-system.svc:8333 refused the connection")
        assert Bucket == "ops" and ContentType == "application/json"
        self.objects[Key] = json.loads(Body)
        return {}

    def phases(self) -> list[str]:
        return [record["phase"] for _key, record in sorted(self.objects.items())]


@pytest.fixture
def dagster() -> Iterator[LaunchingDagster]:
    fake = LaunchingDagster()
    yield fake
    fake.close()


@pytest.fixture
def store() -> AuditStore:
    return AuditStore()


@pytest.fixture
def stdout() -> list[str]:
    return []


def _settings(settings: Settings, **extra: Any) -> Settings:
    raw = {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": list(settings.stages),
    }
    return Settings.from_mapping({**raw, **extra})


@pytest.fixture
def api(settings: Settings, dagster: LaunchingDagster, store: AuditStore, stdout: list[str]) -> TestClient:
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql", "prod": f"{dagster.url}/graphql"},
        audit_base_uri=AUDIT_BASE,
    )
    audit = AuditLog(store, AUDIT_BASE, emit=stdout.append, clock=lambda: NOW)
    return TestClient(create_app(configured, audit=audit))


def _as(mint: Mint, role: str) -> dict[str, str]:
    return bearer(mint(groups=list(ROLE_MAPPING[role]), sub=f"{role}-1", email=f"{role}@example.com"))


def _launch(api: TestClient, headers: dict[str, str], stage: str = "dev", job: str = "orders_job") -> Any:
    return api.post(f"/api/v1/stages/{stage}/runs", json={"job": job}, headers=headers)


@pytest.mark.parametrize("role", ["platform-admin", "data-engineer"])
def test_a_permitted_role_launches_the_job_tagged_with_who_asked_and_both_records_are_stored(
    api: TestClient, mint: Mint, dagster: LaunchingDagster, store: AuditStore, stdout: list[str], role: str
) -> None:
    response = _launch(api, _as(mint, role))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["run_id"] == "run-new" and body["job"] == "orders_job" and body["stage"] == "dev"
    request_id = body["request_id"]
    [params] = dagster.launches
    assert params["selector"] == {
        "repositoryLocationName": "project-code",
        "repositoryName": "__repository__",
        "pipelineName": "orders_job",
    }
    assert params["executionMetadata"]["tags"] == [
        {"key": "openlakeforge/launched-by", "value": f"{role}@example.com"},
        {"key": "openlakeforge/launch-key", "value": request_id},
    ]
    assert sorted(store.objects) == [
        f"audit/platform-api/date=2026-10-31/123005123456-{request_id}-attempted.json",
        f"audit/platform-api/date=2026-10-31/123005123456-{request_id}-succeeded.json",
    ]
    attempted, succeeded = (store.objects[key] for key in sorted(store.objects))
    assert attempted == {
        "schema": "openlakeforge.platform_api_audit.v1",
        "request_id": request_id,
        "phase": "attempted",
        "operation": "launch_run",
        "stage": "dev",
        "subject": f"{role}-1",
        "email": f"{role}@example.com",
        "role": role,
        "target": {"job": "orders_job"},
        "result": {},
        "status": None,
        "detail": "",
        "time": "2026-10-31T12:30:05.123456+00:00",
    }
    assert succeeded["phase"] == "succeeded" and succeeded["result"] == {"run_id": "run-new"}
    assert [json.loads(line) for line in stdout] == [attempted, succeeded]


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_a_role_below_the_floor_is_refused_and_the_refusal_is_stored(
    api: TestClient, mint: Mint, dagster: LaunchingDagster, store: AuditStore, role: str
) -> None:
    response = _launch(api, _as(mint, role))

    assert response.status_code == 403
    assert "it needs 'data-engineer' or above" in response.json()["detail"]
    assert dagster.launches == [] and dagster.variables == []
    [refused] = store.objects.values()
    assert refused["phase"] == "refused" and refused["status"] == 403
    assert refused["operation"] == "launch_run" and refused["stage"] == "dev"
    assert refused["email"] == f"{role}@example.com" and refused["role"] == role


def test_a_stage_outside_the_row_is_refused_even_for_platform_admin_and_the_refusal_is_stored(
    api: TestClient, mint: Mint, dagster: LaunchingDagster, store: AuditStore
) -> None:
    response = _launch(api, _as(mint, "platform-admin"), stage="prod")

    assert response.status_code == 403
    assert response.json()["detail"] == "'launch_run' does not apply to stage 'prod'"
    assert dagster.launches == []
    [refused] = store.objects.values()
    assert refused["phase"] == "refused" and refused["stage"] == "prod" and refused["role"] == "platform-admin"


def test_nothing_is_launched_when_the_attempt_cannot_be_stored(
    api: TestClient, mint: Mint, dagster: LaunchingDagster, store: AuditStore
) -> None:
    store.fail_after = 0

    response = _launch(api, _as(mint, "data-engineer"))

    assert response.status_code == 503
    assert response.json()["detail"] == "the audit record could not be stored; nothing was done"
    # Not even the job list is read: the API does nothing before the attempt is on record.
    assert dagster.variables == [] and dagster.launches == []


def test_an_unknown_job_is_404_and_recorded_as_failed(
    api: TestClient, mint: Mint, dagster: LaunchingDagster, store: AuditStore
) -> None:
    response = _launch(api, _as(mint, "data-engineer"), job="no_such_job")

    assert response.status_code == 404
    assert response.json()["detail"] == "no job 'no_such_job' in stage 'dev'"
    assert dagster.launches == []
    assert store.phases() == ["attempted", "failed"]
    failed = store.objects[sorted(store.objects)[1]]
    assert failed["status"] == 404 and failed["target"] == {"job": "no_such_job"}


def test_an_unreachable_dagster_is_502_and_recorded_as_failed(
    settings: Settings, mint: Mint, store: AuditStore
) -> None:
    configured = _settings(settings, dagster_graphql_urls={"dev": f"{UNREACHABLE}/graphql"}, audit_base_uri=AUDIT_BASE)
    api = TestClient(create_app(configured, audit=AuditLog(store, AUDIT_BASE, emit=lambda _line: None)))

    response = _launch(api, _as(mint, "data-engineer"))

    assert response.status_code == 502
    assert response.json()["detail"] == "Dagster for stage 'dev' is unreachable"
    assert store.phases() == ["attempted", "failed"]


def test_the_launch_key_is_the_apis_own_request_id_and_never_the_clients(
    api: TestClient, mint: Mint, dagster: LaunchingDagster, store: AuditStore
) -> None:
    headers = {**_as(mint, "data-engineer"), "X-Request-Id": "../../evil"}

    response = _launch(api, headers)

    request_id = response.json()["request_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert all(request_id in key and "evil" not in key for key in store.objects)


@pytest.mark.parametrize("job", ["", "orders-job", "a/b", "x" * 256])
def test_a_job_name_dagster_could_not_have_is_refused_before_anything_is_recorded(
    api: TestClient, mint: Mint, dagster: LaunchingDagster, store: AuditStore, job: str
) -> None:
    response = _launch(api, _as(mint, "data-engineer"), job=job)

    assert response.status_code == 422
    assert dagster.launches == [] and store.objects == {}
