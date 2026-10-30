"""Shared base client for consistent HTTP transport, auth, timeout, retry, and error handling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests


class ServiceClientError(RuntimeError):
    """Base error for service client failures."""

    pass


class TransientServiceError(ServiceClientError):
    """Transient service error (5xx, connection, timeout) — retryable."""

    pass


@dataclass
class JsonHttpClient:
    """JSON HTTP client with consistent timeout, auth, and error classification."""

    base_url: str
    timeout: float = 30.0
    token: str | None = None
    # One session per client keeps cookies across calls; Superset binds its
    # CSRF token to the session cookie it sets.
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    def send(
        self,
        method: str,
        path: str,
        *,
        ok_statuses: tuple[int, ...] = (200,),
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an HTTP request and return the raw response.

        Classifies 5xx/connection/timeout as TransientServiceError (retryable),
        other HTTP errors as ServiceClientError.
        """
        all_headers = {"Accept": "application/json", **(headers or {})}
        if self.token:
            all_headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.session.request(
                method, f"{self.base_url}{path}", headers=all_headers, timeout=self.timeout, **kwargs
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TransientServiceError(f"{method} {path} failed: {exc}") from exc

        status = response.status_code
        if status not in ok_statuses:
            if 500 <= status < 600:
                raise TransientServiceError(f"{method} {path} failed with HTTP {status}: {response.text}")
            raise ServiceClientError(f"{method} {path} failed with HTTP {status}: {response.text}")
        return response

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | list[Any] | None = None,
        params: Mapping[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (200,),
        content_type: str = "application/json",
    ) -> Mapping[str, Any]:
        """Make an HTTP request and return the parsed JSON response; errors as in `send`."""
        body = self.send(
            method,
            path,
            json=json_body,
            params=params,
            headers={"Content-Type": content_type} if json_body is not None else None,
            ok_statuses=ok_statuses,
        ).text
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}
