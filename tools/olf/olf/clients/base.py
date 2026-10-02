"""Shared base client for consistent HTTP transport, auth, timeout, retry, and error handling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
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
        """Make an HTTP request and return parsed JSON response.

        Classifies 5xx/connection/timeout as TransientServiceError (retryable),
        other HTTP errors and decode failures as ServiceClientError.
        """
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if json_body is not None:
            headers["Content-Type"] = content_type

        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            status = response.status_code
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TransientServiceError(f"{method} {path} failed: {exc}") from exc

        body = response.text

        if status not in ok_statuses:
            if 500 <= status < 600:
                raise TransientServiceError(f"{method} {path} failed with HTTP {status}: {body}")
            raise ServiceClientError(f"{method} {path} failed with HTTP {status}: {body}")

        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}
