"""The cross-site write guard, and the Trino identity a query route derives from the validated token."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi import Depends
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.query_identity import TrinoIdentity, trino_identity
from openlakeforge_platform_api.settings import Settings, SettingsError

ORIGIN = "https://portal.example.com"
LAUNCH = "/api/v1/stages/dev/runs"
IMPORT = "/api/v1/stages/dev/reports"


@pytest.fixture
def api(settings: Settings) -> TestClient:
    return TestClient(create_app(replace(settings, stages=("dev",), public_origin=ORIGIN)))


# Without a token a request that passes the guard is answered 401; the guard answers 403 first.
def _passes(response: Any) -> bool:
    return response.status_code == 401


def test_a_write_from_another_origin_is_refused(api: TestClient) -> None:
    response = api.post(LAUNCH, json={"job": "j"}, headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403


def test_a_write_from_the_portal_origin_passes(api: TestClient) -> None:
    assert _passes(api.post(LAUNCH, json={"job": "j"}, headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}))


def test_a_write_with_no_origin_is_a_cli_and_passes(api: TestClient) -> None:
    assert _passes(api.post(LAUNCH, json={"job": "j"}))


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_write_the_browser_marks_as_not_same_origin_is_refused(api: TestClient, site: str) -> None:
    assert api.post(LAUNCH, json={"job": "j"}, headers={"Sec-Fetch-Site": site}).status_code == 403


def test_a_write_the_browser_marks_as_user_initiated_passes(api: TestClient) -> None:
    assert _passes(api.post(LAUNCH, json={"job": "j"}, headers={"Sec-Fetch-Site": "none"}))


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "application/zip"])
def test_a_json_route_refuses_a_body_of_another_type(api: TestClient, content_type: str) -> None:
    response = api.post(LAUNCH, content=b'{"job": "j"}', headers={"Content-Type": content_type})
    assert response.status_code == 403


def test_report_import_takes_a_zip_and_nothing_a_form_can_send(api: TestClient) -> None:
    assert _passes(api.post(IMPORT, content=b"PK", headers={"Content-Type": "application/zip"}))
    assert api.post(IMPORT, content=b"a=b", headers={"Content-Type": "text/plain"}).status_code == 403
    assert api.post(IMPORT, content=b"{}", headers={"Content-Type": "application/json"}).status_code == 403


def test_reads_are_never_refused_by_the_guard(api: TestClient) -> None:
    assert api.get("/api/healthz", headers={"Origin": "https://evil.example.com"}).status_code == 200


def test_with_no_public_origin_every_browser_write_is_refused_and_the_cli_still_passes(settings: Settings) -> None:
    api = TestClient(create_app(replace(settings, stages=("dev",))))
    assert api.post(LAUNCH, json={"job": "j"}, headers={"Origin": ORIGIN}).status_code == 403
    assert _passes(api.post(LAUNCH, json={"job": "j"}))


@pytest.mark.parametrize("origin", ["https://portal.example.com/", "portal.example.com", "https://p.example.com/x"])
def test_a_public_origin_with_a_path_or_no_scheme_is_refused_at_startup(settings: Settings, origin: str) -> None:
    with pytest.raises(SettingsError):
        replace(settings, public_origin=origin)._validate()


def _identity_api(settings: Settings, **overrides: Any) -> TestClient:
    def probe(identity: TrinoIdentity = Depends(trino_identity)) -> dict[str, str]:  # noqa: B008
        return {"user": identity.user, "credential": identity.extra_credential}

    matrix = PermissionMatrix.from_mapping(
        {"operations": {"probe": {"role": "viewer", "stages": []}}}, settings.precedence
    )
    app = create_app(replace(settings, **overrides), permissions=matrix)
    app.add_api_route("/api/v1/probe", probe, methods=["GET"], name="probe")
    return TestClient(app)


def _person(mint: Mint, **claims: Any) -> str:
    return mint(
        groups=list(ROLE_MAPPING["viewer"]), federated_claims={"connector_id": "github", "user_id": "1"}, **claims
    )


def test_the_identity_is_the_email_and_the_token_in_the_shape_olf_query_sends(settings: Settings, mint: Mint) -> None:
    token = _person(mint)
    response = _identity_api(settings, person_query=True).get("/api/v1/probe", headers=bearer(token))
    assert response.json() == {"user": "ada@example.com", "credential": f"token={token}"}


@pytest.mark.parametrize(
    "claims",
    [{"email": None}, {"federated_claims": None}, {"federated_claims": "github"}, {"groups": ["unmapped"]}],
)
def test_a_token_missing_what_trino_needs_is_refused_and_never_echoed(
    settings: Settings, mint: Mint, claims: dict[str, Any]
) -> None:
    token = mint(**{"groups": list(ROLE_MAPPING["viewer"]), "federated_claims": {"connector_id": "github"}, **claims})
    response = _identity_api(settings, person_query=True).get("/api/v1/probe", headers=bearer(token))
    assert response.status_code == 403
    assert token not in response.text


def test_where_query_is_off_the_route_refuses_without_looking_at_the_token(settings: Settings, mint: Mint) -> None:
    response = _identity_api(settings).get("/api/v1/probe", headers=bearer(_person(mint)))
    assert response.status_code == 404


def test_the_identity_does_not_print_the_token() -> None:
    assert "secret-token" not in repr(TrinoIdentity("ada@example.com", "secret-token"))
