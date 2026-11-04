"""Query capabilities and per-person schema and table discovery, with a fake Trino behind the statement client."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import pytest
import requests
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import QueryEndpointSetting, Settings, SettingsError

TRINO = "https://trino.example.com/v1/statement"


class FakeTrino:
    """Answers each statement with `pages` (one page per call) or fails it; records what the API sent."""

    def __init__(self) -> None:
        self.rows: list[list[Any]] = []
        self.error: dict[str, Any] | None = None
        self.down = False
        self.sent: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if self.down:
            raise requests.ConnectionError("refused")
        self.sent.append({"method": method, "headers": kwargs["headers"], "sql": (kwargs.get("data") or b"").decode()})
        body: dict[str, Any] = {"error": self.error} if self.error else {"columns": [{"name": "c"}], "data": self.rows}
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response


@pytest.fixture
def trino() -> FakeTrino:
    return FakeTrino()


def _api(settings: Settings, trino: FakeTrino, **overrides: Any) -> TestClient:
    values: dict[str, Any] = {
        "stages": ("dev", "prod"),
        "person_query": True,
        "query_endpoints": {"dev": QueryEndpointSetting(TRINO, "dev")},
    }
    return TestClient(create_app(replace(settings, **{**values, **overrides}), trino_send=trino))


@pytest.fixture
def token(mint: Mint) -> str:
    return mint(groups=list(ROLE_MAPPING["viewer"]), federated_claims={"connector_id": "github", "user_id": "1"})


def test_capabilities_list_only_the_stages_the_deployment_configures(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    response = _api(settings, trino).get("/api/v1/query-capabilities", headers=bearer(token))
    assert response.json() == {
        "query_available": True,
        "stages": [
            {"stage": "dev", "query": {"engine": "trino", "available": True, "export_formats": ["csv", "json"]}}
        ],
    }
    assert "trino.example.com" not in response.text


@pytest.mark.parametrize("overrides", [{"person_query": False}, {"query_endpoints": {}}])
def test_capabilities_say_unavailable_where_there_is_no_query(
    settings: Settings, trino: FakeTrino, token: str, overrides: dict[str, Any]
) -> None:
    response = _api(settings, trino, **overrides).get("/api/v1/query-capabilities", headers=bearer(token))
    assert response.json() == {"query_available": False, "stages": []}


def test_a_stage_that_is_not_enabled_or_offers_no_query_is_404(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    api = _api(settings, trino)
    for stage in ("uat", "prod"):
        assert api.get(f"/api/v1/stages/{stage}/schemas", headers=bearer(token)).status_code == 404
    assert trino.sent == []


def test_discovery_fails_closed_where_person_query_is_off(settings: Settings, trino: FakeTrino, token: str) -> None:
    response = _api(settings, trino, person_query=False).get("/api/v1/stages/dev/schemas", headers=bearer(token))
    assert response.status_code == 404
    assert trino.sent == []


def test_schemas_are_asked_as_the_person_in_the_stage_catalog_without_a_session_schema(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    trino.rows = [["bronze"], ["silver"]]
    response = _api(settings, trino).get("/api/v1/stages/dev/schemas", headers=bearer(token))
    assert response.json() == {"schemas": ["bronze", "silver"], "next_cursor": None}
    headers = trino.sent[0]["headers"]
    assert headers["X-Trino-User"] == "ada@example.com"
    assert headers["X-Trino-Catalog"] == "dev"
    assert headers["Authorization"] == f"Bearer {token}"
    assert "X-Trino-Schema" not in headers
    assert token not in response.text and token not in str(response.headers)


def test_pagination_returns_one_page_and_a_cursor_that_resumes_after_it(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    api = _api(settings, trino)
    trino.rows = [["a"], ["b"], ["c"]]
    first = api.get("/api/v1/stages/dev/schemas?limit=2", headers=bearer(token)).json()
    assert first["schemas"] == ["a", "b"] and first["next_cursor"]
    assert "LIMIT 3" in trino.sent[0]["sql"]
    trino.rows = [["c"]]
    last = api.get(f"/api/v1/stages/dev/schemas?limit=2&cursor={first['next_cursor']}", headers=bearer(token)).json()
    assert last == {"schemas": ["c"], "next_cursor": None}
    assert "schema_name > 'b'" in trino.sent[1]["sql"]


def test_a_page_that_ends_exactly_at_the_limit_has_no_cursor(settings: Settings, trino: FakeTrino, token: str) -> None:
    trino.rows = [["a"], ["b"]]
    assert (
        _api(settings, trino).get("/api/v1/stages/dev/schemas?limit=2", headers=bearer(token)).json()["next_cursor"]
        is None
    )


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "cursor=%FF"])
def test_a_bad_limit_or_cursor_is_refused_before_trino_is_asked(
    settings: Settings, trino: FakeTrino, token: str, query: str
) -> None:
    assert _api(settings, trino).get(f"/api/v1/stages/dev/schemas?{query}", headers=bearer(token)).status_code in (
        400,
        422,
    )
    assert trino.sent == []


def test_search_text_cannot_leave_its_literal_or_act_as_a_wildcard(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    _api(settings, trino).get(f"/api/v1/stages/dev/schemas?q={quote(chr(39) + '%_X')}", headers=bearer(token))
    assert "LIKE '%''\\%\\_x%' ESCAPE '\\'" in trino.sent[0]["sql"]


def test_tables_are_listed_for_one_schema_with_their_kind(settings: Settings, trino: FakeTrino, token: str) -> None:
    trino.rows = [["orders", "BASE TABLE"], ["orders_v", "VIEW"]]
    response = _api(settings, trino).get("/api/v1/stages/dev/schemas/it's/tables", headers=bearer(token))
    assert response.json()["tables"] == [{"name": "orders", "type": "table"}, {"name": "orders_v", "type": "view"}]
    assert "table_schema = 'it''s'" in trino.sent[0]["sql"]


def test_a_catalog_name_with_a_quote_is_quoted_as_an_identifier(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    endpoints = {"dev": QueryEndpointSetting(TRINO, 'we"ird')}
    _api(settings, trino, query_endpoints=endpoints).get("/api/v1/stages/dev/schemas", headers=bearer(token))
    assert '"we""ird".information_schema.schemata' in trino.sent[0]["sql"]


def test_a_denial_is_403_and_not_an_empty_list(settings: Settings, trino: FakeTrino, token: str) -> None:
    trino.error = {"message": "Access Denied: Cannot access catalog dev"}
    response = _api(settings, trino).get("/api/v1/stages/dev/schemas", headers=bearer(token))
    assert response.status_code == 403
    assert "catalog" not in response.text and token not in response.text


def test_a_failed_statement_is_502_without_trinos_text(settings: Settings, trino: FakeTrino, token: str) -> None:
    trino.error = {"message": "line 1:1: boom"}
    response = _api(settings, trino).get("/api/v1/stages/dev/schemas", headers=bearer(token))
    assert response.status_code == 502 and "boom" not in response.text


def test_an_unreachable_trino_is_502(settings: Settings, trino: FakeTrino, token: str) -> None:
    trino.down = True
    response = _api(settings, trino).get("/api/v1/stages/dev/schemas", headers=bearer(token))
    assert response.status_code == 502 and "trino.example.com" not in response.text


def test_endpoints_for_a_stage_that_is_not_enabled_are_refused_at_startup(settings: Settings) -> None:
    with pytest.raises(SettingsError):
        replace(settings, query_endpoints={"uat": QueryEndpointSetting(TRINO, "uat")})._validate()
