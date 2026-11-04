"""Trino statement protocol: submit one statement, follow `nextUri`, collect columns and rows.

The transport is injected so each caller keeps its own TLS trust, timeouts and
loopback handling, and words failures for its own audience: this module raises
typed errors and never phrases advice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from .base import ServiceClientError

# `(method, url, headers=..., data=...)`: the shape of `requests.request`, which is what callers wrap.
Send = Callable[..., requests.Response]


@dataclass(frozen=True)
class Result:
    columns: list[str]
    rows: list[list[Any]]


class TrinoRequestError(ServiceClientError):
    """A request that never produced a query result. `kind` is one of tls, unreachable, unauthenticated,
    forbidden, no_route or bad_response; `host` is where it was sent."""

    def __init__(self, kind: str, host: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(f"{kind}: {host} {detail}".strip(), status=status)
        self.kind = kind
        self.host = host
        self.detail = detail


class TrinoQueryError(ServiceClientError):
    """Trino accepted the statement and then failed it; `error` is its `error` object, unchanged."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message") or "the query failed"))
        self.error = error


def run_statement(
    send: Send,
    url: str,
    sql: str,
    *,
    bearer: Callable[[], str],
    user: str,
    catalog: str,
    source: str,
) -> Result:
    """`bearer` is asked again before every page: a Dex token lasts five minutes, and a result can take longer to
    page through. The extra credential is read once, at submission, where Polaris captures it."""
    headers = {
        "X-Trino-User": user,
        "X-Trino-Catalog": catalog,
        "X-Trino-Source": source,
    }
    token = bearer()
    columns: list[str] = []
    rows: list[list[Any]] = []
    body = _call(
        send,
        "POST",
        url,
        {
            **headers,
            "Authorization": f"Bearer {token}",
            "X-Trino-Extra-Credential": f"token={token}",
        },
        data=sql.encode("utf-8"),
    )
    while True:
        if "error" in body:
            raise TrinoQueryError(body["error"])
        if body.get("columns") and not columns:
            columns = [column["name"] for column in body["columns"]]
        rows.extend(body.get("data") or [])
        if "nextUri" not in body:
            return Result(columns, rows)
        body = _call(
            send,
            "GET",
            body["nextUri"],
            {**headers, "Authorization": f"Bearer {bearer()}"},
        )


def _call(
    send: Send,
    method: str,
    url: str,
    headers: dict[str, str],
    data: bytes | None = None,
) -> dict[str, Any]:
    host = urlsplit(url).hostname or ""
    try:
        response = send(method, url, headers=headers, data=data)
    except requests.exceptions.SSLError as exc:
        raise TrinoRequestError("tls", host) from exc
    except requests.RequestException as exc:
        raise TrinoRequestError("unreachable", host, detail=type(exc).__name__) from exc
    status = response.status_code
    refusal = {401: "unauthenticated", 403: "forbidden", 404: "no_route"}.get(status)
    if refusal:
        raise TrinoRequestError(refusal, host, status=status)
    try:
        body = response.json()
    except ValueError as exc:
        raise TrinoRequestError("bad_response", host, status=status) from exc
    if not isinstance(body, dict):
        raise TrinoRequestError("bad_response", host, status=status)
    return body
