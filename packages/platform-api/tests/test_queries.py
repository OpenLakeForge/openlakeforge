"""Running, paging, cancelling and exporting a person's SQL, with a fake Trino behind the statement client."""

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import replace
from typing import Any

import pytest
import requests
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import QUERY_SIGNING_KEY_ENV, QueryEndpointSetting, Settings

TRINO = "https://trino.example.com/v1/statement"
KEY = "k" * 40
COLUMNS = [{"name": "n", "type": "bigint"}, {"name": "label", "type": "varchar"}]
QUERY_STARTS = ("SELECT", "WITH", "VALUES", "TABLE")


def _error(name: str, kind: str, message: str) -> dict[str, Any]:
    return {"message": message, "errorName": name, "errorType": kind, "failureInfo": {"message": message}}


class FakeTrino:
    """Stands in for Trino: queries are classified by their first word, as Trino's grammar would, and a query's
    result is `pages`, one per continuation URI."""

    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = [{"columns": COLUMNS, "data": [[1, "a"], [2, "b"]]}]
        self.fail: Exception | None = None
        self.sent: list[dict[str, Any]] = []
        self.cancelled = False
        self.runs = 0
        self.redirect_to: str | None = None

    def _reply(self, body: dict[str, Any]) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response

    def _validate(self, text: str) -> dict[str, Any]:
        inner = text.removeprefix("SELECT * FROM (\n").removesuffix("\n)")
        wrapped = text.startswith("SELECT * FROM (")
        word = inner.split()[0].upper() if inner.split() else ""
        if wrapped and word not in QUERY_STARTS:
            return {"error": _error("SYNTAX_ERROR", "USER_ERROR", "line 1:15: mismatched input")}
        if word not in (*QUERY_STARTS, "INSERT", "DROP", "CREATE", "DELETE", "SHOW"):
            return {"error": _error("SYNTAX_ERROR", "USER_ERROR", "line 1:1: mismatched input 'SELEC'")}
        return {"columns": [{"name": "Valid", "type": "boolean"}], "data": [[True]]}

    def _page(self, index: int) -> dict[str, Any]:
        body = dict(self.pages[index])
        body["id"] = "q1"
        if index + 1 < len(self.pages):
            body["nextUri"] = f"https://trino.example.com/v1/statement/executing/q1/slug/{index + 1}"
        return body

    def __call__(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if self.fail:
            raise self.fail
        sql = (kwargs.get("data") or b"").decode()
        self.sent.append({"method": method, "url": url, "headers": kwargs["headers"], "sql": sql})
        if method == "DELETE":
            self.cancelled = True
            return self._reply({})
        if method == "POST":
            if sql.startswith("EXPLAIN (TYPE VALIDATE) "):
                return self._reply(self._validate(sql.removeprefix("EXPLAIN (TYPE VALIDATE) ")))
            self.runs += 1
            if "DENIED" in sql:
                return self._reply({"error": _error("PERMISSION_DENIED", "USER_ERROR", "Access Denied: Cannot select")})
            if "BOOM" in sql:
                return self._reply({"error": _error("GENERIC_INTERNAL_ERROR", "INTERNAL_ERROR", "secret detail")})
            if "BADTABLE" in sql:
                return self._reply({"error": _error("TABLE_NOT_FOUND", "USER_ERROR", "Table 'dev.s.t' does not exist")})
            return self._reply(self._page(0))
        if self.cancelled:
            return self._reply({"error": _error("USER_CANCELED", "USER_ERROR", "Query was canceled")})
        return self._reply(self._page(int(url.rsplit("/", 1)[1])))


@pytest.fixture
def trino() -> FakeTrino:
    return FakeTrino()


@pytest.fixture(autouse=True)
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(QUERY_SIGNING_KEY_ENV, KEY)


def _api(settings: Settings, trino: FakeTrino, **overrides: Any) -> TestClient:
    values: dict[str, Any] = {
        "stages": ("dev", "prod"),
        "person_query": True,
        "query_endpoints": {"dev": QueryEndpointSetting(TRINO, "dev"), "prod": QueryEndpointSetting(TRINO, "prod")},
        "query_wait_seconds": 0.0,
        "public_origin": "https://portal.example.com",
        **overrides,
    }
    return TestClient(create_app(replace(settings, **values), trino_send=trino))


def _token(mint: Mint, sub: str = "user-1", email: str = "ada@example.com") -> str:
    return mint(
        sub=sub,
        email=email,
        groups=list(ROLE_MAPPING["viewer"]),
        federated_claims={"connector_id": "github", "user_id": sub},
    )


@pytest.fixture
def token(mint: Mint) -> str:
    return _token(mint)


@pytest.fixture
def api(settings: Settings, trino: FakeTrino) -> TestClient:
    return _api(settings, trino)


def _submit(api: TestClient, token: str, sql: str = "SELECT n, label FROM s.t", stage: str = "dev") -> Any:
    return api.post(f"/api/v1/stages/{stage}/queries", json={"sql": sql}, headers=bearer(token))


def test_submit_returns_the_rows_and_columns_of_a_finished_query(api: TestClient, token: str) -> None:
    response = _submit(api, token)
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "succeeded"
    assert body["columns"] == COLUMNS
    assert body["rows"] == [[1, "a"], [2, "b"]]
    assert (body["row_count"], body["truncated"], body["next_cursor"]) == (2, False, None)
    assert isinstance(body["elapsed_ms"], int)


def test_a_trailing_semicolon_is_dropped(api: TestClient, trino: FakeTrino, token: str) -> None:
    assert _submit(api, token, "SELECT 1;  ").status_code == 200
    assert trino.sent[-1]["sql"] == "SELECT 1"


def test_every_trino_call_is_the_persons_in_the_stage_catalog_without_a_session_schema(
    api: TestClient, trino: FakeTrino, token: str
) -> None:
    response = _submit(api, token)
    assert trino.sent
    for call in trino.sent:
        assert call["headers"]["X-Trino-User"] == "ada@example.com"
        assert call["headers"]["X-Trino-Catalog"] == "dev"
        assert call["headers"]["Authorization"] == f"Bearer {token}"
        assert "X-Trino-Schema" not in call["headers"]
    assert trino.sent[-1]["headers"]["X-Trino-Extra-Credential"] == f"token={token}"
    assert token not in response.text and token not in str(response.headers)


def _submit_in(api: TestClient, token: str, schema: Any, sql: str = "SELECT n FROM t") -> Any:
    body = {"sql": sql, "schema": schema}
    return api.post("/api/v1/stages/dev/queries", json=body, headers=bearer(token))


def test_a_named_schema_is_sent_on_the_validation_and_the_run(api: TestClient, trino: FakeTrino, token: str) -> None:
    assert _submit_in(api, token, "sales").status_code == 200
    assert len(trino.sent) == 2 and trino.sent[0]["sql"].startswith("EXPLAIN (TYPE VALIDATE)")
    assert [call["headers"]["X-Trino-Schema"] for call in trino.sent] == ["sales", "sales"]


def test_a_write_probe_also_carries_the_schema(api: TestClient, trino: FakeTrino, token: str) -> None:
    assert _submit_in(api, token, "sales", "DROP TABLE t").status_code == 403
    assert trino.sent and all(call["headers"]["X-Trino-Schema"] == "sales" for call in trino.sent)


def test_no_schema_or_null_sends_no_schema_header(api: TestClient, trino: FakeTrino, token: str) -> None:
    assert _submit_in(api, token, None).status_code == 200
    assert all("X-Trino-Schema" not in call["headers"] for call in trino.sent)


@pytest.mark.parametrize("schema", ["a\nb", "a\r\nX-Evil: 1", "a\x00", "a\x7f", "caf\u00e9", "", "x" * 129])
def test_a_schema_that_cannot_be_a_header_value_is_400(
    api: TestClient, trino: FakeTrino, token: str, schema: str
) -> None:
    assert _submit_in(api, token, schema).status_code == 400
    assert trino.sent == []


def test_an_unknown_body_field_is_still_422(api: TestClient, token: str) -> None:
    body = {"sql": "SELECT 1", "schema": "s", "catalog": "x"}
    assert api.post("/api/v1/stages/dev/queries", json=body, headers=bearer(token)).status_code == 422


def test_export_reruns_under_the_schema_the_id_was_signed_with(api: TestClient, trino: FakeTrino, token: str) -> None:
    query_id = _submit_in(api, token, "sales").json()["id"]
    trino.sent.clear()
    export = api.get(f"/api/v1/stages/dev/queries/{query_id}/export", params={"format": "csv"}, headers=bearer(token))
    assert export.status_code == 200
    assert [call["headers"]["X-Trino-Schema"] for call in trino.sent] == ["sales"]


def test_an_id_with_a_changed_schema_is_404(api: TestClient, trino: FakeTrino, token: str) -> None:
    query_id = _submit_in(api, token, "sales").json()["id"]
    body_text, _, mac = query_id.partition(".")
    payload = json.loads(zlib.decompress(base64.urlsafe_b64decode(body_text + "=" * (-len(body_text) % 4))))
    payload["sc"] = "secret"
    forged = base64.urlsafe_b64encode(zlib.compress(json.dumps(payload).encode())).decode().rstrip("=") + "." + mac
    base = f"/api/v1/stages/dev/queries/{forged}"
    assert api.get(base, headers=bearer(token)).status_code == 404
    assert api.get(base + "/export", params={"format": "csv"}, headers=bearer(token)).status_code == 404


def test_a_body_naming_a_catalog_or_endpoint_is_refused(api: TestClient, trino: FakeTrino, token: str) -> None:
    response = api.post(
        "/api/v1/stages/dev/queries", json={"sql": "SELECT 1", "catalog": "other"}, headers=bearer(token)
    )
    assert response.status_code == 422
    assert trino.sent == []


def test_a_query_still_running_is_polled_through_its_cursor_to_the_end(
    api: TestClient, trino: FakeTrino, token: str
) -> None:
    trino.pages = [
        {"stats": {"state": "QUEUED"}},
        {"columns": COLUMNS, "data": [[1, "a"]], "stats": {"state": "RUNNING"}},
        {"columns": COLUMNS, "data": [[2, "b"]], "stats": {"state": "FINISHED"}},
    ]
    first = _submit(api, token).json()
    assert (first["status"], first["rows"]) == ("queued", [])
    second = api.get(
        f"/api/v1/stages/dev/queries/{first['id']}", params={"cursor": first["next_cursor"]}, headers=bearer(token)
    ).json()
    assert (second["status"], second["rows"], second["columns"]) == ("running", [[1, "a"]], COLUMNS)
    third = api.get(
        f"/api/v1/stages/dev/queries/{first['id']}", params={"cursor": second["next_cursor"]}, headers=bearer(token)
    ).json()
    assert (third["status"], third["rows"], third["next_cursor"]) == ("succeeded", [[2, "b"]], None)


def test_with_time_to_wait_one_request_follows_pages_to_the_end(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    trino.pages = [{"stats": {"state": "QUEUED"}}, {"columns": COLUMNS, "data": [[1, "a"]]}]
    body = _submit(_api(settings, trino, query_wait_seconds=5.0), token).json()
    assert (body["status"], body["rows"]) == ("succeeded", [[1, "a"]])


def test_a_page_never_carries_more_rows_than_the_cap_and_says_it_was_truncated(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    trino.pages = [{"columns": COLUMNS, "data": [[i, "x"] for i in range(10)]}, {"data": [[99, "x"]]}]
    body = _submit(_api(settings, trino, query_page_rows=3), token).json()
    assert body["row_count"] == 3 and body["truncated"] is True and body["next_cursor"] is None


def test_cancel_deletes_the_trino_query_and_a_later_poll_reports_cancelled(
    api: TestClient, trino: FakeTrino, token: str
) -> None:
    trino.pages = [{"stats": {"state": "QUEUED"}}, {"columns": COLUMNS, "data": []}]
    first = _submit(api, token).json()
    response = api.delete(f"/api/v1/stages/dev/queries/{first['id']}", headers=bearer(token))
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert [call["method"] for call in trino.sent][-1] == "DELETE"
    polled = api.get(
        f"/api/v1/stages/dev/queries/{first['id']}", params={"cursor": first["next_cursor"]}, headers=bearer(token)
    )
    assert polled.json()["status"] == "cancelled"


@pytest.mark.parametrize("sql", ["INSERT INTO s.t VALUES (1)", "DROP TABLE s.t", "SHOW TABLES"])
def test_a_statement_that_is_not_a_query_is_refused_as_a_write_and_never_run(
    api: TestClient, trino: FakeTrino, token: str, sql: str
) -> None:
    response = _submit(api, token, sql)
    assert response.status_code == 403
    assert "writes" in response.json()["detail"]
    assert trino.runs == 0


def test_a_comment_cannot_turn_a_write_into_a_query(api: TestClient, trino: FakeTrino, token: str) -> None:
    assert _submit(api, token, "INSERT INTO s.t VALUES (1) -- SELECT 1").status_code == 403
    assert trino.runs == 0


def test_invalid_sql_is_400_with_trinos_own_message(api: TestClient, token: str) -> None:
    response = _submit(api, token, "SELEC 1")
    assert response.status_code == 400
    assert "mismatched input 'SELEC'" in response.json()["detail"]


def test_a_query_that_fails_in_trino_for_the_users_own_reasons_is_400(api: TestClient, token: str) -> None:
    response = _submit(api, token, "SELECT * FROM BADTABLE")
    assert response.status_code == 400 and "does not exist" in response.json()["detail"]


def test_an_empty_statement_is_400(api: TestClient, trino: FakeTrino, token: str) -> None:
    assert _submit(api, token, " ; ").status_code == 400
    assert trino.sent == []


def test_a_polaris_or_trino_denial_is_403_and_names_the_denial(api: TestClient, token: str) -> None:
    response = _submit(api, token, "SELECT DENIED")
    assert response.status_code == 403 and "Access Denied" in response.json()["detail"]


def test_an_engine_error_is_502_without_the_engines_detail(api: TestClient, token: str) -> None:
    response = _submit(api, token, "SELECT BOOM")
    assert response.status_code == 502 and "secret detail" not in response.text


@pytest.mark.parametrize(
    ("failure", "status"),
    [(requests.ConnectionError("refused"), 502), (requests.Timeout("slow"), 504)],
)
def test_an_unreachable_engine_is_502_and_a_timeout_504(
    api: TestClient, trino: FakeTrino, token: str, failure: Exception, status: int
) -> None:
    trino.fail = failure
    assert _submit(api, token).status_code == status


def _first(api: TestClient, trino: FakeTrino, token: str) -> dict[str, Any]:
    trino.pages = [{"stats": {"state": "QUEUED"}}, {"columns": COLUMNS, "data": [[1, "a"]]}]
    body: dict[str, Any] = _submit(api, token).json()
    return body


def test_another_persons_id_cursor_and_cancel_are_404(
    api: TestClient, trino: FakeTrino, token: str, mint: Mint
) -> None:
    first = _first(api, trino, token)
    other = bearer(_token(mint, sub="user-2", email="bob@example.com"))
    base = f"/api/v1/stages/dev/queries/{first['id']}"
    assert api.get(base, headers=other).status_code == 404
    assert api.get(base, params={"cursor": first["next_cursor"]}, headers=other).status_code == 404
    assert api.delete(base, headers=other).status_code == 404
    assert api.get(base + "/export", params={"format": "csv"}, headers=other).status_code == 404
    assert trino.cancelled is False


def test_an_id_from_another_stage_is_404(api: TestClient, trino: FakeTrino, token: str) -> None:
    first = _first(api, trino, token)
    assert api.get(f"/api/v1/stages/prod/queries/{first['id']}", headers=bearer(token)).status_code == 404


def test_a_tampered_or_swapped_cursor_is_404(api: TestClient, trino: FakeTrino, token: str) -> None:
    first = _first(api, trino, token)
    base = f"/api/v1/stages/dev/queries/{first['id']}"
    cursor: str = first["next_cursor"]
    forged = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    for bad in (forged, "garbage", first["id"]):  # an id is not a cursor
        assert api.get(base, params={"cursor": bad}, headers=bearer(token)).status_code == 404
    assert api.get(base, params={"cursor": "x." + cursor.split(".")[1]}, headers=bearer(token)).status_code == 404


def test_the_browser_never_receives_trinos_continuation_uri(api: TestClient, trino: FakeTrino, token: str) -> None:
    trino.pages = [{"stats": {"state": "QUEUED"}}, {"data": []}]
    body = _submit(api, token)
    assert body.json()["next_cursor"]
    assert "trino.example.com" not in body.text and "/v1/statement" not in body.text


def test_a_continuation_always_goes_to_the_stages_configured_origin(
    settings: Settings, trino: FakeTrino, token: str
) -> None:
    trino.pages = [{"stats": {"state": "QUEUED"}}, {"columns": COLUMNS, "data": [[1, "a"]]}]
    api = _api(settings, trino)
    first = _submit(api, token).json()
    trino.sent.clear()
    api.get(f"/api/v1/stages/dev/queries/{first['id']}", params={"cursor": first["next_cursor"]}, headers=bearer(token))
    assert trino.sent[0]["url"].startswith("https://trino.example.com/v1/statement/")


def test_without_a_session_the_routes_answer_401(api: TestClient) -> None:
    assert api.post("/api/v1/stages/dev/queries", json={"sql": "SELECT 1"}).status_code == 401


def test_unknown_stage_and_stage_without_an_endpoint_are_404(settings: Settings, trino: FakeTrino, token: str) -> None:
    api = _api(settings, trino, query_endpoints={"dev": QueryEndpointSetting(TRINO, "dev")})
    assert _submit(api, token, stage="uat").status_code == 404
    assert _submit(api, token, stage="prod").status_code == 404
    assert trino.sent == []


def test_where_person_query_is_off_every_route_is_404(settings: Settings, trino: FakeTrino, token: str) -> None:
    api = _api(settings, trino, person_query=False)
    assert _submit(api, token).status_code == 404
    assert api.get("/api/v1/stages/dev/queries/x", headers=bearer(token)).status_code == 404
    assert api.delete("/api/v1/stages/dev/queries/x", headers=bearer(token)).status_code == 404
    assert api.get("/api/v1/stages/dev/queries/x/export?format=csv", headers=bearer(token)).status_code == 404
    assert trino.sent == []


@pytest.mark.parametrize("value", [None, "too-short"])
def test_without_a_signing_key_query_execution_is_unavailable(
    settings: Settings, trino: FakeTrino, token: str, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(QUERY_SIGNING_KEY_ENV)
    else:
        monkeypatch.setenv(QUERY_SIGNING_KEY_ENV, value)
    response = _submit(_api(settings, trino), token)
    assert response.status_code == 503
    assert trino.sent == []


def test_the_csrf_guard_still_applies_to_submit_and_cancel(api: TestClient, trino: FakeTrino, token: str) -> None:
    headers = {**bearer(token), "Origin": "https://evil.example.com"}
    assert api.post("/api/v1/stages/dev/queries", json={"sql": "SELECT 1"}, headers=headers).status_code == 403
    assert api.delete("/api/v1/stages/dev/queries/x", headers=headers).status_code == 403
    form = api.post(
        "/api/v1/stages/dev/queries", content="sql=SELECT 1", headers={**bearer(token), "Content-Type": "text/plain"}
    )
    assert form.status_code == 403
    assert trino.sent == []


def _export(api: TestClient, token: str, fmt: str, sql: str = "SELECT n, label FROM s.t") -> Any:
    query_id = _submit(api, token, sql).json()["id"]
    return api.get(f"/api/v1/stages/dev/queries/{query_id}/export", params={"format": fmt}, headers=bearer(token))


def test_export_csv_downloads_and_neutralises_formulas(api: TestClient, trino: FakeTrino, token: str) -> None:
    trino.pages = [{"columns": COLUMNS, "data": [[1, "=SUM(A1)"], [-2, "@cmd"], [3, None], [4, "a,b"]]}]
    response = _export(api, token, "csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="query-results.csv"'
    assert response.text.splitlines() == ["n,label", "1,'=SUM(A1)", "-2,'@cmd", "3,", '4,"a,b"']


def test_export_json_downloads_the_whole_result(api: TestClient, token: str) -> None:
    response = _export(api, token, "json")
    assert response.headers["content-disposition"] == 'attachment; filename="query-results.json"'
    assert response.json() == {"columns": ["n", "label"], "rows": [[1, "a"], [2, "b"]]}


def test_export_beyond_the_row_limit_is_413_and_sends_nothing(settings: Settings, trino: FakeTrino, token: str) -> None:
    trino.pages = [{"columns": COLUMNS, "data": [[i, "x"] for i in range(5)]}]
    response = _export(_api(settings, trino, query_export_max_rows=3), token, "csv")
    assert response.status_code == 413 and "more than 3 rows" in response.json()["detail"]


def test_export_runs_as_the_person_and_an_unknown_format_is_422(api: TestClient, trino: FakeTrino, token: str) -> None:
    query_id = _submit(api, token).json()["id"]
    assert api.get(f"/api/v1/stages/dev/queries/{query_id}/export?format=xml", headers=bearer(token)).status_code == 422
    _export(api, token, "csv")
    assert trino.sent[-1]["headers"]["X-Trino-User"] == "ada@example.com"


def test_logs_carry_neither_the_sql_nor_the_token(
    api: TestClient, trino: FakeTrino, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    _submit(api, token, "SELECT BOOM, 'secret-literal'")
    trino.fail = requests.ConnectionError("refused")
    _submit(api, token, "SELECT 'secret-literal'")
    assert "secret-literal" not in caplog.text and token not in caplog.text
