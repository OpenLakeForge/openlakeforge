"""The permission matrix: role floors, stage scope, and that no route escapes it."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import PRECEDENCE, ROLE_MAPPING, Mint, bearer
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.permissions import PermissionMatrix, PermissionMatrixError
from openlakeforge_platform_api.settings import Settings

IDENTITY_ROLES = Path(__file__).resolve().parents[3] / "release/identity-roles.yaml"


def _token(mint: Mint, role: str) -> dict[str, str]:
    return bearer(mint(groups=list(ROLE_MAPPING[role])))


def _app(settings: Settings, operations: dict[str, dict[str, object]]) -> FastAPI:
    """The shipped app plus two probe routes, under a matrix the test chooses."""
    matrix = PermissionMatrix.from_mapping({"operations": operations}, settings.precedence)
    app = create_app(settings, permissions=matrix)
    app.add_api_route("/api/v1/probe", lambda: {"ok": True}, name="probe")
    app.add_api_route("/api/v1/stages/{stage}/probe", lambda stage: {"stage": stage}, name="stage_probe")
    return app


def test_the_shipped_matrix_uses_the_release_role_model() -> None:
    precedence = tuple(yaml.safe_load(IDENTITY_ROLES.read_text())["precedence"])

    assert precedence == PRECEDENCE
    PermissionMatrix.packaged(precedence)


def test_every_shipped_route_has_exactly_one_row_and_every_row_a_route(settings: Settings) -> None:
    app = create_app(settings)
    routes = {route.name for route in app.routes if isinstance(route, APIRoute)}

    assert routes == set(app.state.permissions.operations)


def test_the_shipped_app_serves_only_its_read_routes(settings: Settings) -> None:
    routes = [route for route in create_app(settings).routes if isinstance(route, APIRoute)]

    assert sorted((route.path, ",".join(sorted(route.methods or ()))) for route in routes) == [
        ("/api/healthz", "GET"),
        ("/api/v1/data-products", "GET"),
        ("/api/v1/domains", "GET"),
        ("/api/v1/stages/{stage}/jobs", "GET"),
        ("/api/v1/stages/{stage}/runs", "GET"),
        ("/api/v1/stages/{stage}/runs/{run_id}", "GET"),
        ("/api/v1/whoami", "GET"),
    ]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/api/docs", "/api/openapi.json"])
def test_no_framework_route_is_served_outside_the_matrix(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    ("role", "status"),
    [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403), ("viewer", 403)],
)
def test_an_operation_admits_its_floor_and_every_role_above(
    settings: Settings, mint: Mint, role: str, status: int
) -> None:
    client = TestClient(_app(settings, {"probe": {"role": "data-engineer", "stages": []}}))

    assert client.get("/api/v1/probe", headers=_token(mint, role)).status_code == status


def test_a_route_without_a_row_is_refused_even_to_platform_admin(settings: Settings, mint: Mint) -> None:
    client = TestClient(_app(settings, {}))

    response = client.get("/api/v1/probe", headers=_token(mint, "platform-admin"))

    assert response.status_code == 403
    assert "no permission row" in response.json()["detail"]


def test_a_route_without_a_row_is_refused_before_any_token_is_asked_for(settings: Settings) -> None:
    client = TestClient(_app(settings, {}))

    assert client.get("/api/v1/probe").status_code == 403


@pytest.mark.parametrize(("stage", "status"), [("dev", 200), ("prod", 403), ("uat", 404)])
def test_a_stage_scoped_operation_applies_to_its_stages_only(
    settings: Settings, mint: Mint, stage: str, status: int
) -> None:
    client = TestClient(_app(settings, {"stage_probe": {"role": "viewer", "stages": ["dev"]}}))

    assert client.get(f"/api/v1/stages/{stage}/probe", headers=_token(mint, "platform-admin")).status_code == status


def test_every_stage_is_every_enabled_stage(settings: Settings, mint: Mint) -> None:
    client = TestClient(_app(settings, {"stage_probe": {"role": "viewer", "stages": ["*"]}}))

    for stage in settings.stages:
        assert client.get(f"/api/v1/stages/{stage}/probe", headers=_token(mint, "viewer")).status_code == 200
    assert client.get("/api/v1/stages/uat/probe", headers=_token(mint, "viewer")).status_code == 404


def test_an_operation_that_names_no_stage_refuses_a_stage_in_its_path(settings: Settings, mint: Mint) -> None:
    client = TestClient(_app(settings, {"stage_probe": {"role": "viewer", "stages": []}}))

    assert client.get("/api/v1/stages/dev/probe", headers=_token(mint, "platform-admin")).status_code == 403


def test_an_anonymous_operation_takes_no_token(settings: Settings) -> None:
    client = TestClient(_app(settings, {"probe": {"role": "anonymous", "stages": []}}))

    assert client.get("/api/v1/probe").status_code == 200


@pytest.mark.parametrize(
    "operations",
    [
        {"probe": {"role": "owner", "stages": []}},
        {"probe": {"role": "viewer"}},
        {"probe": {"role": "viewer", "stages": [], "note": "extra"}},
        {"probe": {"role": "viewer", "stages": "dev"}},
        {"probe": {"role": "viewer", "stages": [""]}},
        {"probe": {"role": "anonymous", "stages": ["dev"]}},
    ],
)
def test_a_malformed_matrix_refuses_to_load(operations: dict[str, dict[str, object]]) -> None:
    with pytest.raises(PermissionMatrixError):
        PermissionMatrix.from_mapping({"operations": operations}, PRECEDENCE)


def test_a_role_outside_precedence_is_never_allowed() -> None:
    matrix = PermissionMatrix.from_mapping({"operations": {"probe": {"role": "viewer", "stages": []}}}, PRECEDENCE)

    assert not matrix.allows("probe", "owner")
    assert not matrix.allows("probe", None)
    assert not matrix.allows("unknown", "platform-admin")
