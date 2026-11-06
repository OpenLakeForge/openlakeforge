"""The shared Trino statement client, driven through a fake transport that stands in for Trino."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests
from openlakeforge_services.trino import (
    TrinoAuthError,
    TrinoCancelledError,
    TrinoForbiddenError,
    TrinoNotFoundError,
    TrinoProtocolError,
    TrinoQueryError,
    TrinoStatementClient,
    TrinoTimeoutError,
    TrinoTlsError,
    TrinoUnreachableError,
)

URL = "https://trino.example.com/v1/statement"
SECRET = "eyJ.secret.token"


class FakeTrino:
    """Answers each request from a script of (status, body) or an exception, and records what was sent."""

    def __init__(self, *answers: tuple[int, Any] | tuple[int, Any, dict[str, str]] | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        response = requests.Response()
        response.status_code, body, *headers = answer
        response._content = json.dumps(body).encode()
        response.headers.update(headers[0] if headers else {})
        return response


def _client(fake: FakeTrino) -> TrinoStatementClient:
    return TrinoStatementClient(URL, catalog="dev", user="ada@example.com", source="test", send=fake)


def _page(rows: list[list[Any]], next_uri: str | None = None, columns: bool = True) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {"data": rows}
    if columns:
        body["columns"] = [{"name": "id"}]
    if next_uri:
        body["nextUri"] = next_uri
    return 200, body


def test_run_collects_every_page_and_cancels_nothing() -> None:
    fake = FakeTrino(_page([[1]], f"{URL}/1"), _page([[2]], columns=False))

    result = _client(fake).run("SELECT 1", bearer=lambda: SECRET)

    assert (result.columns, result.rows, result.truncated) == (["id"], [[1], [2]], False)
    assert [call["method"] for call in fake.calls] == ["POST", "GET"]


def test_rows_past_the_limit_are_dropped_flagged_and_the_query_cancelled() -> None:
    fake = FakeTrino(_page([[1], [2]], f"{URL}/1"), _page([[3]], f"{URL}/2"), (204, {}))

    result = _client(fake).run("SELECT 1", bearer=lambda: SECRET, max_rows=2)

    assert (result.rows, result.truncated) == ([[1], [2]], True)
    assert fake.calls[-1]["method"] == "DELETE"
    assert fake.calls[-1]["url"] == f"{URL}/2"


def test_a_result_exactly_at_the_limit_is_not_truncated() -> None:
    fake = FakeTrino(_page([[1], [2]], f"{URL}/1"), _page([]))

    result = _client(fake).run("SELECT 1", bearer=lambda: SECRET, max_rows=2)

    assert (result.rows, result.truncated) == ([[1], [2]], False)
    assert all(call["method"] != "DELETE" for call in fake.calls)


@pytest.mark.parametrize(
    "next_uri",
    [
        "https://evil.example.com/v1/statement/1",
        "http://trino.example.com/v1/statement/1",
        "https://trino.example.com:8443/x",
    ],
)
def test_a_continuation_off_the_configured_origin_is_refused_before_any_request(next_uri: str) -> None:
    fake = FakeTrino(_page([[1]], next_uri))

    with pytest.raises(TrinoProtocolError) as refused:
        _client(fake).run("SELECT 1", bearer=lambda: SECRET)

    assert len(fake.calls) == 1
    assert SECRET not in str(refused.value)


def test_a_continuation_with_credentials_in_the_authority_is_refused() -> None:
    fake = FakeTrino(_page([[1]], "https://trino.example.com@evil.example.com/v1/statement/1"))

    with pytest.raises(TrinoProtocolError):
        _client(fake).run("SELECT 1", bearer=lambda: SECRET)


def test_cancel_refuses_a_foreign_uri() -> None:
    with pytest.raises(TrinoProtocolError):
        _client(FakeTrino()).cancel("https://evil.example.com/v1/statement/1", SECRET)


def test_cancelling_a_query_that_is_already_gone_succeeds() -> None:
    _client(FakeTrino((404, {}))).cancel(f"{URL}/1", SECRET)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ((401, {}), TrinoAuthError),
        ((403, {}), TrinoForbiddenError),
        ((404, {}), TrinoNotFoundError),
        ((502, ["not", "a", "result"]), TrinoProtocolError),
        (requests.exceptions.SSLError("bad"), TrinoTlsError),
        (requests.exceptions.ReadTimeout("slow"), TrinoTimeoutError),
        (requests.exceptions.ConnectionError(f"refused {SECRET}"), TrinoUnreachableError),
    ],
)
def test_transport_and_http_failures_map_to_typed_errors_without_the_token(
    answer: tuple[int, Any] | Exception, expected: type[Exception]
) -> None:
    with pytest.raises(expected) as raised:
        _client(FakeTrino(answer)).submit("SELECT 1", SECRET)

    assert SECRET not in str(raised.value)


def test_a_timeout_is_also_unreachable() -> None:
    with pytest.raises(TrinoUnreachableError):
        _client(FakeTrino(requests.exceptions.ReadTimeout("slow"))).submit("SELECT 1", SECRET)


def test_a_polaris_denial_is_told_from_a_sql_error_and_from_a_cancellation() -> None:
    denial = {"message": "Failed", "failureInfo": {"message": "Forbidden: not authorized for op LIST_TABLES"}}
    syntax = {"message": "line 1:8: Column 'x' cannot be resolved", "errorName": "COLUMN_NOT_FOUND"}
    cancelled = {"message": "Query was canceled", "errorName": "USER_CANCELED"}
    outcomes: list[TrinoQueryError] = []
    for error in (denial, syntax, cancelled):
        with pytest.raises(TrinoQueryError) as raised:
            _client(FakeTrino((200, {"error": error}))).submit("SELECT 1", SECRET)
        outcomes.append(raised.value)

    assert [e.denied for e in outcomes] == [True, False, False]
    assert [isinstance(e, TrinoCancelledError) for e in outcomes] == [False, False, True]
    assert outcomes[0].messages == ["Failed", "Forbidden: not authorized for op LIST_TABLES"]


def test_only_the_submission_carries_the_extra_credentials_and_each_page_a_fresh_bearer() -> None:
    fake = FakeTrino(_page([[1]], f"{URL}/1"), _page([[2]], columns=False))
    tokens = iter(["first", "refreshed"])

    _client(fake).run("SELECT 1", bearer=lambda: next(tokens), extra_credentials=lambda t: {"token": t})

    submitted, page = fake.calls[0]["headers"], fake.calls[1]["headers"]
    assert (submitted["Authorization"], submitted["X-Trino-Extra-Credential"]) == ("Bearer first", "token=first")
    assert page["Authorization"] == "Bearer refreshed"
    assert "X-Trino-Extra-Credential" not in page
    assert (submitted["X-Trino-Catalog"], submitted["X-Trino-User"], submitted["X-Trino-Source"]) == (
        "dev",
        "ada@example.com",
        "test",
    )
    assert fake.calls[0]["allow_redirects"] is False


def test_basic_credentials_authenticate_a_workload_principal_and_replace_the_bearer() -> None:
    import base64

    fake = FakeTrino(_page([[1]], f"{URL}/1"), _page([[2]], columns=False))
    client = TrinoStatementClient(
        URL, catalog="dev", user="olf-prod-runtime", source="test", send=fake, basic=("olf-dev-runtime", "s3cret")
    )

    client.run("SELECT 1", bearer=lambda: SECRET)

    expected = "Basic " + base64.b64encode(b"olf-dev-runtime:s3cret").decode()
    assert [call["headers"]["Authorization"] for call in fake.calls] == [expected, expected]
    # The claimed user is the client's, apart from the authenticated principal.
    assert {call["headers"]["X-Trino-User"] for call in fake.calls} == {"olf-prod-runtime"}
    assert SECRET not in json.dumps([call["headers"] for call in fake.calls])


def test_no_bearer_and_no_basic_credentials_send_no_authorization_header() -> None:
    fake = FakeTrino(_page([[1]]))

    _client(fake).run("SELECT 1", bearer=lambda: "")

    assert "Authorization" not in fake.calls[0]["headers"]


def test_a_shared_session_carries_use_set_session_and_a_transaction_to_the_next_statement() -> None:
    from openlakeforge_services.trino import TrinoSession

    session = TrinoSession()
    use = {
        "X-Trino-Set-Schema": "order_revenue_gold",
        "X-Trino-Set-Session": "query_max_stage_count=300,time_zone=Europe%2FParis",
        "X-Trino-Started-Transaction-Id": "tx-1",
    }
    fake = FakeTrino((200, {"id": "q1"}, use), (200, {"id": "q2"}))
    client = TrinoStatementClient(URL, catalog="dev", user="u", source="test", send=fake, session=session)

    client.run("USE order_revenue_gold", bearer=lambda: "")
    client.run("SELECT 1", bearer=lambda: "")

    first, second = (call["headers"] for call in fake.calls)
    assert "X-Trino-Schema" not in first
    # Open transactions are announced as none until Trino starts one.
    assert first["X-Trino-Transaction-Id"] == "NONE"
    assert second["X-Trino-Schema"] == "order_revenue_gold"
    assert second["X-Trino-Transaction-Id"] == "tx-1"
    assert second["X-Trino-Session"] == "query_max_stage_count=300,time_zone=Europe%2FParis"
    assert second["X-Trino-Catalog"] == "dev"


def test_a_session_forgets_what_trino_clears() -> None:
    from openlakeforge_services.trino import TrinoSession

    session = TrinoSession()
    session.update(
        {"X-Trino-Set-Session": "a=1,b=2", "X-Trino-Started-Transaction-Id": "tx", "X-Trino-Set-Catalog": "c2"}
    )
    session.update({"X-Trino-Clear-Session": "a", "X-Trino-Clear-Transaction-Id": "true"})

    assert (session.catalog, session.properties, session.transaction_id) == ("c2", {"b": "2"}, None)


def test_without_a_shared_session_every_request_is_independent() -> None:
    fake = FakeTrino((200, {"id": "q1"}, {"X-Trino-Set-Schema": "other"}), (200, {"id": "q2"}))
    client = _client(fake)

    client.run("USE other", bearer=lambda: "")
    client.run("SELECT 1", bearer=lambda: "")

    assert "X-Trino-Schema" not in fake.calls[1]["headers"]


def test_only_session_values_are_form_encoded_and_the_schema_is_kept_as_sent() -> None:
    from openlakeforge_services.trino import TrinoSession

    session = TrinoSession()
    # Trino 480 sends the schema as it is (`a+b` is a schema named a+b) and form-encodes only a session value,
    # a space being `+`.
    session.update({"X-Trino-Set-Session": "query_max_execution_time=1+h", "X-Trino-Set-Schema": "a+b"})
    fake = FakeTrino((200, {"id": "q"}))
    client = TrinoStatementClient(URL, catalog="dev", user="u", source="test", send=fake, session=session)

    client.run("SELECT 1", bearer=lambda: "")

    headers = fake.calls[0]["headers"]
    assert session.properties == {"query_max_execution_time": "1 h"}
    assert session.schema == "a+b"
    assert headers["X-Trino-Schema"] == "a+b"
    # The value goes back percent-encoded, which Trino decodes to the same space; the name is not encoded.
    assert headers["X-Trino-Session"] == "query_max_execution_time=1%20h"


def test_a_catalog_scoped_property_name_and_a_percent_in_a_schema_are_not_decoded() -> None:
    from openlakeforge_services.trino import TrinoSession

    session = TrinoSession()
    session.update({"X-Trino-Set-Session": "lakehouse_dev.some_flag=true", "X-Trino-Set-Schema": "100%25"})
    session.update({"X-Trino-Clear-Session": "lakehouse_dev.some_flag"})

    assert session.schema == "100%25"
    assert session.properties == {}


def test_a_session_asks_to_start_transactions_and_forgets_one_that_ends() -> None:
    from openlakeforge_services.trino import TrinoSession

    fake = FakeTrino(
        (200, {"id": "q1"}, {"X-Trino-Started-Transaction-Id": "tx-9"}),
        (200, {"id": "q2"}, {"X-Trino-Clear-Transaction-Id": "true"}),
        (200, {"id": "q3"}),
    )
    client = TrinoStatementClient(URL, catalog="dev", user="u", source="test", send=fake, session=TrinoSession())

    for statement in ("START TRANSACTION", "COMMIT", "SELECT 1"):
        client.run(statement, bearer=lambda: "")

    assert [call["headers"]["X-Trino-Transaction-Id"] for call in fake.calls] == ["NONE", "tx-9", "NONE"]


def test_a_requested_capability_is_advertised_and_otherwise_none_is() -> None:
    fake = FakeTrino((200, {"id": "q1"}), (200, {"id": "q2"}))

    _client(fake).run("SELECT 1", bearer=lambda: "")
    TrinoStatementClient(
        URL, catalog="dev", user="u", source="test", send=fake, capabilities=("PARAMETRIC_DATETIME",)
    ).run("SELECT 1", bearer=lambda: "")

    plain, asking = (call["headers"] for call in fake.calls)
    assert "X-Trino-Client-Capabilities" not in plain
    assert asking["X-Trino-Client-Capabilities"] == "PARAMETRIC_DATETIME"


def test_prepared_statements_are_carried_in_the_form_trino_sent_them_and_dropped_when_deallocated() -> None:
    from openlakeforge_services.trino import TrinoSession

    session = TrinoSession()
    fake = FakeTrino(
        (200, {"id": "q1"}, {"X-Trino-Added-Prepare": "p%201=SELECT+%3F+FROM+t"}),
        (200, {"id": "q2"}, {"X-Trino-Deallocated-Prepare": "p%201"}),
        (200, {"id": "q3"}),
    )
    client = TrinoStatementClient(URL, catalog="dev", user="u", source="test", send=fake, session=session)

    for statement in ("PREPARE p 1 FROM SELECT ? FROM t", "EXECUTE p 1 USING 1", "SELECT 1"):
        client.run(statement, bearer=lambda: "")

    headers = [call["headers"] for call in fake.calls]
    assert "X-Trino-Prepared-Statement" not in headers[0]
    # Name and statement go back exactly as Trino wrote them, which is how it decodes them.
    assert headers[1]["X-Trino-Prepared-Statement"] == "p%201=SELECT+%3F+FROM+t"
    assert "X-Trino-Prepared-Statement" not in headers[2]
