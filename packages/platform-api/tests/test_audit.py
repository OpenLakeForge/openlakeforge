"""Audit records for state-changing operations, against a probe route the matrix marks `changes_state`."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.audit import AuditLog, audited
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.settings import Settings, SettingsError

AUDIT_BASE = "s3://ops/audit/platform-api/"
NOW = datetime(2026, 10, 31, 12, 30, 5, 123456, tzinfo=UTC)


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

    def records(self) -> list[dict[str, Any]]:
        return [self.objects[key] for key in sorted(self.objects)]


class Probe:
    """A state-changing route: `fail` makes its body answer 409 or raise; `ran` counts bodies that ran."""

    def __init__(self) -> None:
        self.ran = 0
        self.fail = ""

    def __call__(self, request: Request, stage: str) -> dict[str, str]:
        with audited(request, stage=stage, target={"thing": "t-1"}) as outcome:
            self.ran += 1
            if self.fail == "conflict":
                raise HTTPException(status_code=409, detail="already there")
            if self.fail == "crash":
                raise RuntimeError("boom")
            outcome.result["made"] = "m-1"
        return {"made": "m-1"}


@pytest.fixture
def store() -> AuditStore:
    return AuditStore()


@pytest.fixture
def stdout() -> list[str]:
    return []


@pytest.fixture
def probe() -> Probe:
    return Probe()


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


def _api(settings: Settings, probe: Probe, audit: AuditLog) -> TestClient:
    matrix = PermissionMatrix.from_mapping(
        {"operations": {"probe": {"role": "data-engineer", "stages": ["dev"], "changes_state": True}}},
        settings.precedence,
    )
    app = create_app(_settings(settings, audit_base_uri=AUDIT_BASE), permissions=matrix, audit=audit)
    app.add_api_route("/api/v1/stages/{stage}/probe", probe, methods=["POST"], name="probe")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def api(settings: Settings, probe: Probe, store: AuditStore, stdout: list[str]) -> TestClient:
    return _api(settings, probe, AuditLog(store, AUDIT_BASE, emit=stdout.append, clock=lambda: NOW))


def _as(mint: Mint, role: str) -> dict[str, str]:
    return bearer(mint(groups=list(ROLE_MAPPING[role]), sub=f"{role}-1", email=f"{role}@example.com"))


def test_a_permitted_call_stores_its_attempt_and_its_outcome_and_prints_both(
    api: TestClient, mint: Mint, store: AuditStore, stdout: list[str], probe: Probe
) -> None:
    response = api.post("/api/v1/stages/dev/probe", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200, response.text
    assert probe.ran == 1
    [attempted_key, succeeded_key] = sorted(store.objects)
    request_id = attempted_key.split("-")[-2]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert attempted_key == f"audit/platform-api/date=2026-10-31/123005123456-{request_id}-attempted.json"
    assert succeeded_key == f"audit/platform-api/date=2026-10-31/123005123456-{request_id}-succeeded.json"
    attempted, succeeded = store.records()
    assert attempted == {
        "schema": "openlakeforge.platform_api_audit.v1",
        "request_id": request_id,
        "phase": "attempted",
        "operation": "probe",
        "stage": "dev",
        "subject": "data-engineer-1",
        "email": "data-engineer@example.com",
        "role": "data-engineer",
        "target": {"thing": "t-1"},
        "result": {},
        "status": None,
        "detail": "",
        "time": "2026-10-31T12:30:05.123456+00:00",
    }
    assert succeeded == {**attempted, "phase": "succeeded", "result": {"made": "m-1"}}
    assert [json.loads(line) for line in stdout] == [attempted, succeeded]


@pytest.mark.parametrize(
    ("role", "stage", "detail"),
    [
        ("analyst", "dev", "role 'analyst' may not perform 'probe'; it needs 'data-engineer' or above"),
        ("platform-admin", "prod", "'probe' does not apply to stage 'prod'"),
    ],
)
def test_a_refused_role_or_stage_stores_the_refusal_and_runs_nothing(
    api: TestClient, mint: Mint, store: AuditStore, probe: Probe, role: str, stage: str, detail: str
) -> None:
    response = api.post(f"/api/v1/stages/{stage}/probe", headers=_as(mint, role))

    assert response.status_code == 403
    assert response.json()["detail"] == detail
    assert probe.ran == 0
    [refused] = store.records()
    assert refused["phase"] == "refused" and refused["status"] == 403 and refused["detail"] == detail
    assert refused["stage"] == stage and refused["role"] == role and refused["email"] == f"{role}@example.com"


def test_a_person_with_no_role_is_refused_and_the_refusal_is_stored(
    api: TestClient, mint: Mint, store: AuditStore
) -> None:
    response = api.post("/api/v1/stages/dev/probe", headers=bearer(mint(groups=["acme:strangers"])))

    assert response.status_code == 403
    [refused] = store.records()
    assert refused["subject"] == "user-1" and refused["role"] is None


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-token"}])
def test_a_caller_without_a_valid_token_is_refused_on_stdout_only_so_it_cannot_fill_the_bucket(
    api: TestClient, store: AuditStore, stdout: list[str], probe: Probe, headers: dict[str, str]
) -> None:
    response = api.post("/api/v1/stages/dev/probe", headers=headers)

    assert response.status_code == 401
    assert store.objects == {} and probe.ran == 0
    [line] = [json.loads(line) for line in stdout]
    assert line["phase"] == "refused" and line["status"] == 401 and line["subject"] is None


def test_nothing_runs_when_the_attempt_cannot_be_stored(
    api: TestClient, mint: Mint, store: AuditStore, probe: Probe
) -> None:
    store.fail_after = 0

    response = api.post("/api/v1/stages/dev/probe", headers=_as(mint, "data-engineer"))

    assert response.status_code == 503
    assert response.json()["detail"] == "the audit record could not be stored; nothing was done"
    assert probe.ran == 0
    # The store's own message, which names its endpoint, stays out of the answer.
    assert "seaweedfs" not in response.text


def test_without_an_audit_key_every_state_changing_call_is_refused_with_503(
    settings: Settings, mint: Mint, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_AUDIT_ACCESS_KEY_ID", raising=False)
    matrix = PermissionMatrix.from_mapping(
        {"operations": {"probe": {"role": "data-engineer", "stages": ["dev"], "changes_state": True}}},
        settings.precedence,
    )
    app = create_app(_settings(settings, audit_base_uri=AUDIT_BASE), permissions=matrix)
    app.add_api_route("/api/v1/stages/{stage}/probe", probe, methods=["POST"], name="probe")

    response = TestClient(app).post("/api/v1/stages/dev/probe", headers=_as(mint, "data-engineer"))

    assert response.status_code == 503
    assert probe.ran == 0


def test_a_lost_outcome_record_does_not_change_an_answer_whose_action_happened(
    api: TestClient, mint: Mint, store: AuditStore, stdout: list[str], probe: Probe
) -> None:
    store.fail_after = 1

    response = api.post("/api/v1/stages/dev/probe", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert probe.ran == 1
    assert [record["phase"] for record in store.records()] == ["attempted"]
    # The outcome still reached stdout, where the pod's log keeps it.
    assert [json.loads(line)["phase"] for line in stdout] == ["attempted", "succeeded"]


@pytest.mark.parametrize(
    ("fail", "status", "detail"), [("conflict", 409, "already there"), ("crash", 500, "RuntimeError")]
)
def test_an_operation_that_fails_is_recorded_with_how_it_failed(
    api: TestClient, mint: Mint, store: AuditStore, probe: Probe, fail: str, status: int, detail: str
) -> None:
    probe.fail = fail

    response = api.post("/api/v1/stages/dev/probe", headers=_as(mint, "data-engineer"))

    assert response.status_code == status
    attempted, failed = store.records()
    assert attempted["phase"] == "attempted"
    assert failed["phase"] == "failed" and failed["status"] == status and failed["detail"] == detail


def test_the_request_id_is_the_apis_own_and_never_the_clients(api: TestClient, mint: Mint, store: AuditStore) -> None:
    api.post("/api/v1/stages/dev/probe", headers={**_as(mint, "data-engineer"), "X-Request-Id": "../../evil"})

    assert store.objects and all("evil" not in key for key in store.objects)
    assert len({record["request_id"] for record in store.records()}) == 1


def test_a_read_is_never_audited(settings: Settings, mint: Mint, store: AuditStore, stdout: list[str]) -> None:
    configured = _settings(settings, audit_base_uri=AUDIT_BASE)
    api = TestClient(create_app(configured, audit=AuditLog(store, AUDIT_BASE, emit=stdout.append)))

    assert api.get("/api/v1/whoami", headers=_as(mint, "viewer")).status_code == 200
    assert api.get("/api/v1/whoami").status_code == 401
    assert store.objects == {} and stdout == []


@pytest.mark.parametrize("uri", ["ops/audit/", "s3://ops/audit", "s3:///audit/"])
def test_an_audit_location_that_is_not_an_s3_prefix_stops_the_api_starting(settings: Settings, uri: str) -> None:
    with pytest.raises(SettingsError, match="audit_base_uri"):
        _settings(settings, audit_base_uri=uri)
