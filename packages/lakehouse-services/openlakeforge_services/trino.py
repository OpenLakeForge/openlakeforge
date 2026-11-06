"""Trino statement protocol client: submit, follow `nextUri`, cancel, and collect a bounded result.

The endpoint, the person's bearer token and the extra credentials Polaris reads are the caller's to inject. Nothing
here reads settings or keeps state between calls, so the platform API can share one client across requests.
Messages carry hostnames and Trino's own error text, never a token.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from .base import ServiceClientError

DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Failure text that means an access decision rather than a mistake in the SQL.
_DENIAL_MARKERS = (
    "Forbidden:",
    "Access Denied",
    "Cannot access catalog",
    "cannot become user",
)


class TrinoError(ServiceClientError):
    """Base of every failure this client raises."""


class TrinoUnreachableError(TrinoError):
    """The request never got an HTTP answer."""


class TrinoTimeoutError(TrinoUnreachableError):
    pass


class TrinoTlsError(TrinoUnreachableError):
    """The certificate did not verify; the caller knows which CA its deployment uses."""


class TrinoAuthError(TrinoError):
    """HTTP 401: Trino or the perimeter refused the token."""


class TrinoForbiddenError(TrinoError):
    """HTTP 403: the perimeter refused the person, before Trino ran anything. Not a Polaris denial."""


class TrinoNotFoundError(TrinoError):
    """HTTP 404: the deployment has no such route."""


class TrinoProtocolError(TrinoError):
    """Trino answered something that is not a statement response, or pointed off its own origin."""


class TrinoQueryError(TrinoError):
    """Trino ran the statement and failed it. `error` is Trino's error object, kept for callers that explain it."""

    def __init__(self, error: Mapping[str, Any]) -> None:
        self.error = dict(error)
        self.messages = list(dict.fromkeys(failure_messages(error)))
        super().__init__(
            "\n".join(self.messages) or "the query failed without an error message."
        )

    @property
    def denied(self) -> bool:
        """An access decision by Polaris or Trino's rules, as against invalid SQL."""
        return any(marker in str(self) for marker in _DENIAL_MARKERS)


class TrinoCancelledError(TrinoQueryError):
    pass


def failure_messages(error: Mapping[str, Any]) -> Iterator[str]:
    """The messages of Trino's failure chain, outermost first: the reason is often only in a cause."""
    if error.get("message"):
        yield str(error["message"])
    info = error.get("failureInfo")
    while isinstance(info, dict):
        if info.get("message"):
            yield str(info["message"])
        info = info.get("cause")


@dataclass(frozen=True)
class TrinoPage:
    columns: list[str]
    rows: list[list[Any]]
    next_uri: str | None
    # Trino's SQL type per column, its query id, and its progress state (QUEUED, RUNNING, FINISHED, ...), for callers
    # that show them; `state` is Trino's own stats field, not part of the documented protocol, so it may be None.
    types: list[str] = field(default_factory=list)
    query_id: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class TrinoResult:
    columns: list[str]
    rows: list[list[Any]]
    # True when rows beyond `max_rows` existed and were dropped.
    truncated: bool = False


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return (
        parts.scheme,
        (parts.hostname or "").lower(),
        parts.port or _DEFAULT_PORTS.get(parts.scheme),
    )


@dataclass(frozen=True)
class TrinoStatementClient:
    """`url` is the statement endpoint (`.../v1/statement`); continuation URIs must stay on its origin.

    `send` is the HTTP transport, `requests.request`-shaped, for a caller that must resolve or trust hosts its own
    way."""

    url: str
    catalog: str
    user: str
    source: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    send: Callable[..., requests.Response] = field(default=requests.request, repr=False)
    # The default schema for unqualified names (`X-Trino-Schema`); None sends no header, as `olf query` does.
    schema: str | None = None
    # A workload principal's (user, password) for Trino's PASSWORD authentication (ADR 0016). It replaces the bearer
    # and is sent as-is: the caller names an address it trusts, because the Python Trino client refuses a password
    # over HTTP and this one does not.
    basic: tuple[str, str] | None = None

    def submit(
        self, sql: str, bearer: str, extra_credentials: Mapping[str, str] | None = None
    ) -> TrinoPage:
        """The extra credentials ride only here: Polaris captures them when the query starts."""
        headers = self._headers(bearer)
        if extra_credentials:
            headers["X-Trino-Extra-Credential"] = ",".join(
                f"{k}={v}" for k, v in extra_credentials.items()
            )
        return self._page("POST", self.url, headers, sql.encode("utf-8"))

    def follow(self, next_uri: str, bearer: str) -> TrinoPage:
        return self._page("GET", self._checked(next_uri), self._headers(bearer))

    def cancel(self, next_uri: str, bearer: str) -> None:
        """Trino's cancellation is a DELETE of the continuation URI; a query already gone (404) is cancelled."""
        try:
            self._call("DELETE", self._checked(next_uri), self._headers(bearer), None)
        except TrinoNotFoundError:
            pass

    def run(
        self,
        sql: str,
        *,
        bearer: Callable[[], str],
        extra_credentials: Callable[[str], Mapping[str, str]] | None = None,
        max_rows: int | None = None,
    ) -> TrinoResult:
        """`bearer` is asked again before every page: a Dex token lasts five minutes, and a result can take longer to
        page through. `extra_credentials` maps the submission's bearer to the credentials sent with it (Polaris reads
        the person's own token). Past `max_rows` the query is cancelled and the result says it was truncated."""
        token = bearer()
        page = self.submit(
            sql, token, extra_credentials(token) if extra_credentials else None
        )
        columns = page.columns
        rows: list[list[Any]] = []
        while True:
            rows.extend(page.rows)
            if max_rows is not None and len(rows) > max_rows:
                if page.next_uri:
                    self._cancel_quietly(page.next_uri, bearer())
                return TrinoResult(columns, rows[:max_rows], truncated=True)
            if page.next_uri is None:
                return TrinoResult(columns, rows)
            page = self.follow(page.next_uri, bearer())
            columns = columns or page.columns

    def _cancel_quietly(self, next_uri: str, bearer: str) -> None:
        # The rows already collected are good; Trino abandons an unpolled query on its own.
        try:
            self.cancel(next_uri, bearer)
        except TrinoError:
            pass

    def _headers(self, bearer: str) -> dict[str, str]:
        headers = {
            "X-Trino-User": self.user,
            "X-Trino-Catalog": self.catalog,
            "X-Trino-Source": self.source,
        }
        if self.basic is not None:
            credential = base64.b64encode(f"{self.basic[0]}:{self.basic[1]}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {credential}"
        elif bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if self.schema:
            headers["X-Trino-Schema"] = self.schema
        return headers

    def _checked(self, uri: str) -> str:
        # The URI comes from the response body: followed blindly, it sends the person's token wherever Trino says.
        if "@" in urlsplit(uri).netloc or _origin(uri) != _origin(self.url):
            raise TrinoProtocolError(
                f"Trino pointed at {urlsplit(uri).hostname or 'a malformed address'}, not at itself"
            )
        return uri

    def _page(
        self, method: str, url: str, headers: dict[str, str], data: bytes | None = None
    ) -> TrinoPage:
        body = self._call(method, url, headers, data)
        error = body.get("error")
        if error is not None:
            cancelled = (
                isinstance(error, dict) and error.get("errorName") == "USER_CANCELED"
            )
            raise (TrinoCancelledError if cancelled else TrinoQueryError)(
                error if isinstance(error, dict) else {}
            )
        described = body.get("columns") or []
        stats = body.get("stats")
        return TrinoPage(
            [column["name"] for column in described],
            body.get("data") or [],
            body.get("nextUri"),
            [str(column.get("type", "")) for column in described],
            body.get("id"),
            stats.get("state") if isinstance(stats, dict) else None,
        )

    def _call(
        self, method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> dict[str, Any]:
        host = urlsplit(url).hostname
        try:
            # No redirects: one would carry the bearer to wherever it points.
            response = self.send(
                method,
                url,
                headers=headers,
                data=data,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            raise TrinoTlsError(f"cannot verify the TLS certificate of {host}") from exc
        except requests.Timeout as exc:
            raise TrinoTimeoutError(
                f"cannot reach Trino at {host}: {type(exc).__name__}"
            ) from exc
        except requests.RequestException as exc:
            raise TrinoUnreachableError(
                f"cannot reach Trino at {host}: {type(exc).__name__}"
            ) from exc
        status = response.status_code
        refusals: dict[int, type[TrinoError]] = {
            401: TrinoAuthError,
            403: TrinoForbiddenError,
            404: TrinoNotFoundError,
        }
        if status in refusals:
            raise refusals[status](f"Trino answered HTTP {status}", status=status)
        if method == "DELETE" and status < 300:
            return {}
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise TrinoProtocolError(
                f"Trino answered HTTP {status} without a query result", status=status
            )
        return body
