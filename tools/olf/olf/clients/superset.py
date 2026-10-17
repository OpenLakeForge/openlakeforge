"""Superset REST client: dashboard listing, report export, and report import.

Endpoints are those of the Superset version pinned in
release/component-catalog.yaml (`superset_base`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .base import JsonHttpClient, ServiceClientError, TransientServiceError


@dataclass
class SupersetClient(JsonHttpClient):
    """Superset REST API client authenticated as a database user."""

    username: str = "admin"
    password: str = "admin"

    def login(self) -> None:
        """Log in and set the bearer token, waiting out a Superset that is still starting."""
        last_error: Exception | None = None
        for _ in range(60):
            try:
                response = self.request(
                    "POST",
                    "/api/v1/security/login",
                    json_body={"username": self.username, "password": self.password, "provider": "db", "refresh": True},
                )
            except TransientServiceError as exc:
                last_error = exc
                time.sleep(2)
                continue
            self.token = str(response["access_token"])
            return
        raise ServiceClientError(f"Superset login failed: {last_error}")

    def dashboards(self) -> list[Mapping[str, Any]]:
        """List all dashboards via the REST API."""
        self.login()
        result = self.request("GET", "/api/v1/dashboard/", params={"q": '{"page_size": 100}'}).get("result", [])
        return list(result)

    def export_dashboard(self, dashboard: str) -> bytes:
        """Export one dashboard, selected by uuid or slug, as a Superset export zip."""
        # `/dashboard/<id_or_slug>` resolves a uuid as well as a slug (DashboardDAO.get_by_id_or_slug);
        # `/dashboard/export/` only takes numeric ids.
        dashboard_id = self.request("GET", f"/api/v1/dashboard/{quote(dashboard, safe='')}")["result"]["id"]
        return self.send("GET", "/api/v1/dashboard/export/", params={"q": f"!({int(dashboard_id)})"}).content

    def import_assets(self, bundle: Path) -> None:
        """Import a `type: assets` bundle, overwriting every asset it carries.

        `/assets/import/` runs ImportAssetsCommand, which always overwrites and
        accepts the `type: assets` metadata report bundles declare;
        `/dashboard/import/` validates the metadata type as `Dashboard` and
        would refuse them.
        """
        # Flask-WTF CSRF protection covers the REST API too; the token is tied to
        # the session cookie the csrf_token call sets. Referer is checked once
        # Superset is served over HTTPS (WTF_CSRF_SSL_STRICT).
        csrf_token = self.request("GET", "/api/v1/security/csrf_token/")["result"]
        with bundle.open("rb") as body:
            self.send(
                "POST",
                "/api/v1/assets/import/",
                files={"bundle": (bundle.name, body, "application/zip")},
                headers={"X-CSRFToken": csrf_token, "Referer": self.base_url},
            )

    def grant_permissions(self, role: str, permission: str, view_menus: Sequence[str]) -> None:
        """Add `permission` on each view menu to `role`, keeping what the role already holds.

        Superset's role-permission endpoint replaces the whole set, so the
        grant is the union with the current set: repeating it changes nothing,
        and a partial import never revokes the schemas of another bundle.
        """
        found = self.request("GET", "/api/v1/security/roles/search/", params={"q": _filter("name", role)})
        role_id = next((r["id"] for r in found.get("result", []) if r["name"] == role), None)
        if role_id is None:
            raise ServiceClientError(f"Superset role {role!r} does not exist; `superset init` creates it")
        wanted: set[int] = set()
        for view_menu in view_menus:
            rows = self.request(
                "GET",
                "/api/v1/security/permissions-resources/",
                params={"q": _filter("view_menu.name", view_menu)},
            ).get("result", [])
            ids = [
                int(r["id"])
                for r in rows
                if r["permission"]["name"] == permission and r["view_menu"]["name"] == view_menu
            ]
            if not ids:
                raise ServiceClientError(f"Superset has no {permission} permission on {view_menu!r}; was it imported?")
            wanted.update(ids)
        held = self.request("GET", f"/api/v1/security/roles/{role_id}/permissions/").get("result", [])
        self.request(
            "POST",
            f"/api/v1/security/roles/{role_id}/permissions",
            json_body={"permission_view_menu_ids": sorted(wanted | {int(r["id"]) for r in held})},
        )


def _filter(column: str, value: str) -> str:
    return json.dumps({"filters": [{"col": column, "opr": "eq", "value": value}], "page_size": 100})
