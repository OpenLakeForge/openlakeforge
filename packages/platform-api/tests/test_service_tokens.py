"""A pod's ServiceAccount token as a service principal (ADR 0021, decision 3), with the TokenReview faked."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import jwt
import pytest
from conftest import ROLE_MAPPING, FakeIssuer, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.permissions import PermissionMatrix, PermissionMatrixError
from openlakeforge_platform_api.service_tokens import ReviewUnavailable, ServiceTokenValidator
from openlakeforge_platform_api.settings import ServiceCallerSetting, Settings, SettingsError

AUDIENCE = "openlakeforge-platform-api"
EVALUATOR = "system:serviceaccount:olf-system:alert-evaluator"


def _service_token() -> str:
    # A projected token is a JWT from the cluster's own issuer; the fake review never reads its content.
    return jwt.encode(
        {"iss": "https://kubernetes.default.svc", "sub": EVALUATOR},
        "unused-key-of-sufficient-length-0123456789",
        "HS256",
    )


class FakeReview:
    def __init__(self, status: Mapping[str, Any] | None = None, fail: bool = False) -> None:
        self.status = status if status is not None else _status()
        self.fail = fail
        self.asked: list[tuple[str, str]] = []

    def __call__(self, token: str, audience: str) -> Mapping[str, Any]:
        self.asked.append((token, audience))
        if self.fail:
            raise ReviewUnavailable("down")
        return self.status


def _status(
    username: str = EVALUATOR, audiences: list[str] | None = None, authenticated: bool = True
) -> dict[str, Any]:
    return {"authenticated": authenticated, "user": {"username": username}, "audiences": audiences or [AUDIENCE]}


@pytest.fixture
def with_callers(settings: Settings) -> Settings:
    return replace(settings, service_callers={"alert-evaluator": ServiceCallerSetting("olf-system", "alert-evaluator")})


def _client(settings: Settings, review: FakeReview) -> TestClient:
    app = create_app(settings, service_validator=ServiceTokenValidator(settings, review))
    return TestClient(app)


def _health(client: TestClient, token: str, stage: str = "dev") -> int:
    return client.get(f"/api/v1/stages/{stage}/health", headers=bearer(token)).status_code


def test_the_evaluators_token_reads_stage_health(with_callers: Settings) -> None:
    review = FakeReview()
    client = _client(with_callers, review)

    assert _health(client, _service_token()) == 200
    assert _health(client, _service_token(), "prod") == 200
    assert review.asked[0][1] == AUDIENCE


def test_every_other_route_refuses_the_service(with_callers: Settings) -> None:
    client = _client(with_callers, FakeReview())
    matrix = client.app.state.permissions  # type: ignore[attr-defined]

    others = [name for name in matrix.operations if name not in ("get_stage_health", "healthz")]
    assert others
    assert not any(matrix.allows(name, None, "dev", service="alert-evaluator") for name in others)
    assert client.get("/api/v1/whoami", headers=bearer(_service_token())).status_code == 403
    assert client.get("/api/v1/stages/dev/health/pipelines", headers=bearer(_service_token())).status_code == 403


@pytest.mark.parametrize(
    "status",
    [
        _status(username="system:serviceaccount:olf-system:default"),
        _status(username="system:serviceaccount:dev:alert-evaluator"),
        _status(audiences=["https://kubernetes.default.svc"]),
        _status(authenticated=False),
        {},
    ],
    ids=["other-sa", "other-namespace", "wrong-audience", "unauthenticated", "no-status"],
)
def test_any_other_identity_is_refused(with_callers: Settings, status: dict[str, Any]) -> None:
    assert _health(_client(with_callers, FakeReview(status)), _service_token()) == 401


def test_an_unreachable_review_fails_closed(with_callers: Settings) -> None:
    client = _client(with_callers, FakeReview(fail=True))

    assert _health(client, _service_token()) == 503
    assert client.get("/api/v1/whoami", headers=bearer(_service_token())).status_code == 503


def test_without_a_configured_caller_a_service_token_is_just_an_invalid_token(settings: Settings) -> None:
    review = FakeReview()

    assert _health(_client(settings, review), _service_token()) == 401
    assert review.asked == []


def test_issuer_tokens_are_unaffected_and_never_reviewed(with_callers: Settings, mint: Mint) -> None:
    review = FakeReview()
    client = _client(with_callers, review)
    engineer = bearer(mint(groups=list(ROLE_MAPPING["data-engineer"])))
    viewer = bearer(mint(groups=list(ROLE_MAPPING["viewer"])))

    assert client.get("/api/v1/whoami", headers=viewer).status_code == 200
    assert client.get("/api/v1/stages/dev/health", headers=viewer).status_code == 403
    assert client.get("/api/v1/stages/dev/health", headers=engineer).status_code in (200, 404, 502)
    assert review.asked == []


def test_a_forged_issuer_claim_gets_no_further_than_the_issuers_checks(
    with_callers: Settings, issuer: FakeIssuer
) -> None:
    forged = jwt.encode({"iss": issuer.url, "sub": EVALUATOR}, "unused-key-of-sufficient-length-0123456789", "HS256")
    review = FakeReview()

    assert _health(_client(with_callers, review), forged) == 401
    assert review.asked == []


def test_the_matrix_names_the_service_only_on_stage_health() -> None:
    matrix = PermissionMatrix.packaged(("platform-admin", "data-engineer", "analyst", "viewer"))

    named = {name for name, row in matrix.operations.items() if "alert-evaluator" in row.services}
    assert named == {"get_stage_health"}
    assert matrix.allows("get_stage_health", None, "dev", service="alert-evaluator")
    assert not matrix.allows("get_stage_health", None, "dev", service="someone-else")
    assert not matrix.allows("get_stage_health", None, "dev")


def test_a_row_cannot_hand_a_service_a_write_or_an_unknown_name() -> None:
    precedence = ("platform-admin", "viewer")
    base = {"role": "viewer", "stages": ["*"]}
    with pytest.raises(PermissionMatrixError):
        PermissionMatrix.from_mapping({"operations": {"x": {**base, "services": ["intruder"]}}}, precedence)
    with pytest.raises(PermissionMatrixError):
        PermissionMatrix.from_mapping(
            {"operations": {"x": {**base, "services": ["alert-evaluator"], "changes_state": True}}}, precedence
        )


def test_settings_reject_an_unknown_service_principal(settings: Settings) -> None:
    with pytest.raises(SettingsError):
        replace(settings, service_callers={"root": ServiceCallerSetting("a", "b")})._validate()
