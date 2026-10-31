"""Unified OpenMetadata client for deploy and e2e paths."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .base import JsonHttpClient, ServiceClientError, TransientServiceError


class OpenMetadataError(ServiceClientError):
    """OpenMetadata-specific client error."""

    pass


class OpenMetadataTransientError(TransientServiceError):
    """OpenMetadata transient error (5xx, connection, timeout)."""

    pass


@dataclass(frozen=True)
class DataProduct:
    name: str
    display_name: str
    # Names of the domains the product belongs to; OpenMetadata 1.13 allows several.
    domains: tuple[str, ...]


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

    def register_self(self, name: str, email: str) -> None:
        """Create the token holder's own user, as its browser sign-in would have.

        OpenMetadata creates a user only on the browser SSO callback, so a
        person who has only used the CLI is unknown to it and every request
        fails. Self-signup (`enableSelfSignup`) lets a token create exactly
        the user it names; `name` is the `preferred_username` OpenMetadata
        keys users on (ADR 0021, decision 2). A 409 means it now exists."""
        self.request("POST", "/api/v1/users", json_body={"name": name, "email": email}, ok_statuses=(200, 201, 409))

    def list_domains(self, *, page_size: int = 100) -> list[tuple[str, str]]:
        """Every domain's name and display name, following the `paging.after` cursor to the last page."""
        return [
            (domain["name"], domain.get("displayName") or "")
            for domain in self._list_all("/api/v1/domains", {}, page_size)
        ]

    def list_data_products(self, *, page_size: int = 100) -> list[DataProduct]:
        """Every data product with the domains it belongs to, across all pages."""
        return [
            DataProduct(
                name=product["name"],
                display_name=product.get("displayName") or "",
                domains=tuple(domain["name"] for domain in product.get("domains") or []),
            )
            # `domains` is not returned unless asked for; 1.13.6's DataProductResource lists it in FIELDS.
            for product in self._list_all("/api/v1/dataProducts", {"fields": "domains"}, page_size)
        ]

    def _list_all(self, path: str, params: Mapping[str, Any], page_size: int) -> list[Mapping[str, Any]]:
        """Every entity of a collection, following the `paging.after` cursor to the last page."""
        entities: list[Mapping[str, Any]] = []
        after = ""
        while True:
            query: dict[str, Any] = {**params, "limit": page_size}
            if after:
                query["after"] = after
            page = self.request("GET", path, params=query)
            entities += page.get("data", [])
            after = page.get("paging", {}).get("after") or ""
            if not after:
                return entities

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
            raise OpenMetadataTransientError(str(exc), status=exc.status) from exc
        except ServiceClientError as exc:
            raise OpenMetadataError(str(exc), status=exc.status) from exc
