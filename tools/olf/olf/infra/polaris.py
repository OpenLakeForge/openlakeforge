"""Polaris client for namespace reconciliation over the Iceberg REST API.

See `olf.infra.catalog` for why this exists and the provider-neutral planner it
implements: `plan_namespace_sync` / `apply_namespace_sync` treat this client
as a `NamespaceClient` and do not otherwise know they are talking to Polaris.
The REST calls below are the ones the old Phase 1 bootstrap Job made with
curl; only the host moved.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from openlakeforge_domain import CatalogNamespace

from olf.infra.catalog import CATALOG_KEY, MANAGED_BY_KEY, MANAGED_BY_VALUE, NamespaceState

LOCATION_PROPERTY = "location"
TOKEN_REQUEST_ATTEMPTS = 60
TOKEN_REQUEST_RETRY_DELAY_SECONDS = 2


class PolarisError(RuntimeError):
    """Raised when Polaris rejects a request or is unreachable."""


@dataclass(frozen=True)
class PolarisConfig:
    """Everything needed to reach one Polaris catalog as the deployer principal."""

    base_url: str
    catalog_name: str
    client_id: str
    client_secret: str
    oauth_scope: str = "PRINCIPAL_ROLE:ALL"

    @property
    def token_path(self) -> str:
        return "/api/catalog/v1/oauth/tokens"

    @property
    def catalog_path(self) -> str:
        return f"/api/catalog/v1/{urllib.parse.quote(self.catalog_name, safe='')}"


@dataclass
class PolarisClient:
    """Minimal Iceberg REST catalog client for namespace reconciliation."""

    config: PolarisConfig
    token: str | None = field(default=None)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        ok_statuses: tuple[int, ...] = (200,),
        relogged: bool = False,
    ) -> dict:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(
            f"{self.config.base_url}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - localhost forward
                body = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            status = err.code
        except urllib.error.URLError as err:
            raise PolarisError(f"{method} {path} failed: {err}") from err

        if status == 401 and self.token and not relogged:
            # Polaris rejects a token whose `iat` lies ahead of its clock, with no leeway, so a VM clock
            # stepping back (a WSL2 resync) fails a token minted a moment earlier. A new one is not.
            self.login()
            return self._request(method, path, payload=payload, ok_statuses=ok_statuses, relogged=True)
        if status not in ok_statuses:
            raise PolarisError(f"{method} {path} failed with HTTP {status}: {body}")
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}

    def login(self) -> None:
        """Exchange the deployer client credentials for a bearer token."""
        form = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": self.config.oauth_scope}
        ).encode("utf-8")
        basic = base64.b64encode(
            f"{self.config.client_id}:{self.config.client_secret}".encode()
        ).decode("ascii")
        request = urllib.request.Request(
            f"{self.config.base_url}{self.config.token_path}",
            data=form,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        for attempt in range(1, TOKEN_REQUEST_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - localhost forward
                    body = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as err:
                detail = err.read().decode("utf-8", errors="replace")
                raise PolarisError(f"Polaris token request failed with HTTP {err.code}: {detail}") from err
            except urllib.error.URLError as err:
                if attempt == TOKEN_REQUEST_ATTEMPTS:
                    raise PolarisError(
                        f"Polaris token request failed after {TOKEN_REQUEST_ATTEMPTS} attempts: {err}"
                    ) from err
                time.sleep(TOKEN_REQUEST_RETRY_DELAY_SECONDS)

        token = body.get("access_token")
        if not token:
            raise PolarisError("Polaris token response contained no access_token")
        self.token = token

    def _namespace_path(self, name: str) -> str:
        return f"{self.config.catalog_path}/namespaces/{urllib.parse.quote(name, safe='')}"

    def list_namespaces(self) -> dict[str, NamespaceState]:
        """Return every top-level namespace mapped to its `location` property.

        Multi-level namespaces are not part of this model -- Glue has no nesting
        and Trino's Iceberg connector flattens to a single schema level -- so a
        nested namespace is a foreign object this command must not touch.
        """
        names: list[str] = []
        page_token = ""
        while True:
            query = f"?pageToken={urllib.parse.quote(page_token, safe='')}" if page_token else ""
            response = self._request("GET", f"{self.config.catalog_path}/namespaces{query}")
            names.extend(levels[0] for levels in response.get("namespaces", []) if len(levels) == 1)
            page_token = response.get("next-page-token") or response.get("nextPageToken") or ""
            if not page_token:
                break
        return {name: self.namespace_state(name) for name in names}

    def namespace_state(self, name: str) -> NamespaceState:
        response = self._request("GET", self._namespace_path(name))
        properties = response.get("properties") or {}
        return NamespaceState(properties.get(LOCATION_PROPERTY, ""), properties)

    def create_namespace(self, namespace: CatalogNamespace) -> None:
        self._request(
            "POST",
            f"{self.config.catalog_path}/namespaces",
            payload={
                "namespace": [namespace.name],
                "properties": self._properties(namespace),
            },
            ok_statuses=(200, 409),
        )

    def update_namespace_location(self, namespace: CatalogNamespace) -> None:
        self._request(
            "POST",
            f"{self._namespace_path(namespace.name)}/properties",
            payload={"removals": [], "updates": self._properties(namespace)},
            ok_statuses=(200,),
        )

    def _properties(self, namespace: CatalogNamespace) -> dict[str, str]:
        return {
            LOCATION_PROPERTY: namespace.location,
            MANAGED_BY_KEY: MANAGED_BY_VALUE,
            CATALOG_KEY: self.config.catalog_name,
        }

    def adopt_namespace(self, namespace: CatalogNamespace) -> None:
        self.update_namespace_location(namespace)

    def drop_namespace(self, name: str) -> None:
        """Remove catalog metadata only; Iceberg object-store files are retained."""
        page_token = ""
        tables: list[str] = []
        while True:
            query = f"?pageToken={urllib.parse.quote(page_token, safe='')}" if page_token else ""
            response = self._request("GET", f"{self._namespace_path(name)}/tables{query}")
            identifiers = response.get("identifiers", [])
            tables.extend(table["name"] if isinstance(table, dict) else table[-1] for table in identifiers)
            page_token = response.get("next-page-token") or response.get("nextPageToken") or ""
            if not page_token:
                break
        for table in tables:
            encoded_table = urllib.parse.quote(table, safe="")
            table_path = f"{self._namespace_path(name)}/tables/{encoded_table}?purgeRequested=false"
            self._request("DELETE", table_path, ok_statuses=(200, 204, 404))
        try:
            self._request("DELETE", self._namespace_path(name), ok_statuses=(200, 204, 404))
        except PolarisError as err:
            raise PolarisError(
                f"Polaris refused to remove metadata for namespace {name!r}: {err}."
            ) from err
