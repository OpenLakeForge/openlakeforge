"""Authentication off (ADR 0021, decision 8): no issuer, and a caller with no token is the anonymous principal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from conftest import PRECEDENCE, bearer
from fastapi import HTTPException
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings, SettingsError


def _settings(**overrides: Any) -> Settings:
    return Settings.from_mapping(
        {
            "precedence": list(PRECEDENCE),
            "stages": ["dev"],
            "public_origin": "http://olf.localhost",
            "anonymous_role": PRECEDENCE[0],
            **overrides,
        }
    )


def test_a_caller_with_no_token_is_the_anonymous_principal_with_the_configured_role() -> None:
    client = TestClient(create_app(_settings()))
    response = client.get("/api/v1/whoami")
    assert response.status_code == 200
    assert response.json()["subject"] == "anonymous"
    assert response.json()["role"] == PRECEDENCE[0]


def test_a_token_is_refused_rather_than_ignored() -> None:
    client = TestClient(create_app(_settings()))
    assert client.get("/api/v1/whoami", headers=bearer("x.y.z")).status_code == 401


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"public_origin": "https://olf.example.com"}, "localhost"),
        ({"public_origin": None}, "localhost"),
        ({"anonymous_role": "nobody"}, "precedence"),
        ({"issuer_url": "http://auth.olf.localhost"}, "issuer_url"),
        ({"audiences": ["olf-cli"]}, "audiences"),
    ],
)
def test_authentication_is_off_only_on_localhost_and_never_beside_an_issuer(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        _settings(**overrides)


def test_without_anonymous_role_an_issuer_is_still_required() -> None:
    with pytest.raises(SettingsError, match="issuer_url is empty"):
        _settings(anonymous_role=None)


def test_the_anonymous_principal_queries_with_the_anonymous_catalog_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_queries import KEY, TRINO, FakeTrino

    from openlakeforge_platform_api.settings import QUERY_SIGNING_KEY_ENV

    monkeypatch.setenv(QUERY_SIGNING_KEY_ENV, KEY)
    trino = FakeTrino()
    endpoints = {"dev": {"statement_url": TRINO, "catalog": "lakehouse_dev"}}
    settings = _settings(person_query=True, query_endpoints=endpoints)
    client = TestClient(create_app(settings, trino_send=trino, anonymous_catalog_token=lambda: "polaris-token"))

    assert client.get("/api/v1/query-capabilities").json()["query_available"] is True
    response = client.post("/api/v1/stages/dev/queries", json={"sql": "SELECT 1"})

    assert response.status_code == 200, response.text
    assert trino.sent and all(sent["headers"]["X-Trino-User"] == "anonymous" for sent in trino.sent)
    submitted = [sent["headers"] for sent in trino.sent if sent["method"] == "POST"]
    assert all(headers["X-Trino-Extra-Credential"] == "token=polaris-token" for headers in submitted)


def test_the_anonymous_catalog_token_is_minted_once_until_it_nears_expiry(tmp_path: Path) -> None:
    from openlakeforge_platform_api.anonymous_catalog import CLIENT_ID, CLIENT_SECRET, AnonymousCatalogToken

    now = [0.0]
    posts: list[dict[str, Any]] = []

    def post(url: str, data: dict[str, Any], timeout: float) -> requests.Response:
        posts.append(data)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"access_token": f"t{len(posts)}", "expires_in": 3600}).encode()
        return response

    mint = AnonymousCatalogToken("http://polaris/token", directory=tmp_path, post=post, clock=lambda: now[0])
    with pytest.raises(HTTPException) as missing:
        mint()
    assert missing.value.status_code == 503 and posts == []

    (tmp_path / CLIENT_ID).write_text("id\n")
    (tmp_path / CLIENT_SECRET).write_text("secret")
    assert (mint(), mint()) == ("t1", "t1")
    assert posts[0]["client_id"] == "id" and posts[0]["grant_type"] == "client_credentials"
    now[0] = 3600 - 59
    assert mint() == "t2"
