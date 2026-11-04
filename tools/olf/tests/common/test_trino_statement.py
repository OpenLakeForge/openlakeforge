"""The Trino statement protocol in `openlakeforge_services.trino`, driven through an injected transport."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests
from openlakeforge_services import trino

URL = "https://trino.example.com/v1/statement"


class Reply:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


def transport(*replies: Reply | Exception) -> tuple[trino.Send, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    queue = iter(replies)

    def send(method: str, url: str, **kwargs: Any) -> Any:
        sent.append({"method": method, "url": url, **kwargs})
        reply = next(queue)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return send, sent


def run(send: trino.Send, bearer: Callable[[], str] = lambda: "tok") -> trino.Result:
    return trino.run_statement(send, URL, "SELECT 1", bearer=bearer, user="ada@example.com", catalog="dev", source="t")


def test_pages_are_followed_and_columns_come_from_the_first_that_has_them() -> None:
    send, sent = transport(
        Reply(body={"columns": [{"name": "id"}], "data": [[1]], "nextUri": "https://trino.example.com/next"}),
        Reply(body={"data": [[2]]}),
    )

    result = run(send)

    assert result == trino.Result(["id"], [[1], [2]])
    assert [(call["method"], call["url"]) for call in sent] == [
        ("POST", URL),
        ("GET", "https://trino.example.com/next"),
    ]
    assert sent[0]["data"] == b"SELECT 1"
    assert sent[0]["headers"]["X-Trino-Source"] == "t"


def test_every_page_asks_for_a_current_token_and_only_the_submission_carries_the_extra_credential() -> None:
    send, sent = transport(Reply(body={"nextUri": "https://trino.example.com/next"}), Reply(body={}))
    tokens = iter(["first", "refreshed"])

    run(send, lambda: next(tokens))

    submitted, page = (call["headers"] for call in sent)
    assert (submitted["Authorization"], submitted["X-Trino-Extra-Credential"]) == ("Bearer first", "token=first")
    assert page["Authorization"] == "Bearer refreshed"
    assert "X-Trino-Extra-Credential" not in page


def test_a_failed_query_carries_trinos_error_object() -> None:
    error = {"message": "boom", "failureInfo": {"message": "cause"}}
    send, _ = transport(Reply(body={"error": error}))

    with pytest.raises(trino.TrinoQueryError) as raised:
        run(send)

    assert raised.value.error == error


@pytest.mark.parametrize(
    ("reply", "kind", "status"),
    [
        (Reply(401), "unauthenticated", 401),
        (Reply(403), "forbidden", 403),
        (Reply(404), "no_route", 404),
        (Reply(502), "bad_response", 502),
        (Reply(200, ["not", "an", "object"]), "bad_response", 200),
        (requests.exceptions.SSLError("x"), "tls", None),
        (requests.ConnectionError("x"), "unreachable", None),
    ],
)
def test_a_request_that_gives_no_result_is_classified(reply: Reply | Exception, kind: str, status: int | None) -> None:
    send, _ = transport(reply)

    with pytest.raises(trino.TrinoRequestError) as raised:
        run(send)

    assert (raised.value.kind, raised.value.status, raised.value.host) == (kind, status, "trino.example.com")
