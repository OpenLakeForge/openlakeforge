"""Unified OpenMetadata client for deploy and e2e paths."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from .base import JsonHttpClient, ServiceClientError, TransientServiceError


class OpenMetadataError(ServiceClientError):
    """OpenMetadata-specific client error."""

    pass


class OpenMetadataTransientError(TransientServiceError):
    """OpenMetadata transient error (5xx, connection, timeout)."""

    pass


class OpenMetadataClient(JsonHttpClient):
    """Unified OpenMetadata REST API client with login and request methods."""

    def login(self, email: str, password: str) -> None:
        """Log in and set bearer token on the client."""
        encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
        try:
            response = self.request(
                "POST",
                "/api/v1/users/login",
                json_body={"email": email, "password": encoded_password},
            )
            token = response.get("accessToken")
            if not token:
                raise OpenMetadataError(f"OpenMetadata login did not return an access token: {response}")
            self.token = token
        except ServiceClientError as exc:
            raise OpenMetadataError(f"OpenMetadata login failed: {exc}") from exc

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
        """Make an HTTP request, wrapping errors in OpenMetadataError."""
        try:
            return super().request(
                method,
                path,
                json_body=json_body,
                params=params,
                ok_statuses=ok_statuses,
                content_type=content_type,
            )
        except TransientServiceError as exc:
            raise OpenMetadataTransientError(str(exc)) from exc
        except ServiceClientError as exc:
            raise OpenMetadataError(str(exc)) from exc
