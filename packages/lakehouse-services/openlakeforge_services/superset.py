"""Superset REST client: dashboard listing, report export, and report import.

Endpoints are those of the Superset version pinned in
release/component-catalog.yaml (`superset_base`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from zipfile import ZipFile

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

    def import_assets(self, bundle: Path, *, database_password: str = "", sparse: bool = False) -> None:
        """Import a `type: assets` bundle, overwriting every asset it carries.

        `/assets/import/` runs ImportAssetsCommand, which always overwrites and
        accepts the `type: assets` metadata report bundles declare;
        `/dashboard/import/` validates the metadata type as `Dashboard` and
        would refuse them.

        `database_password` is sent in the `passwords` form field, which Superset
        applies to each database in the bundle and stores encrypted, so the
        password is never written into the zip.

        `sparse` resolves every asset the bundle refers to but does not carry,
        databases above all, among those Superset already holds, by uuid. A
        bundle imported sparsely needs no database, and so cannot replace one.
        """
        # Flask-WTF CSRF protection covers the REST API too; the token is tied to
        # the session cookie the csrf_token call sets. Referer is checked once
        # Superset is served over HTTPS (WTF_CSRF_SSL_STRICT).
        csrf_token = self._csrf_token()
        form: dict[str, str] = {"sparse": "true"} if sparse else {}
        if database_password:
            with ZipFile(bundle) as archive:
                names = [PurePosixPath(*PurePosixPath(n).parts[1:]).as_posix() for n in archive.namelist()]
            form["passwords"] = json.dumps(
                {name: database_password for name in names if name.startswith("databases/")}
            )
        with bundle.open("rb") as body:
            self.send(
                "POST",
                "/api/v1/assets/import/",
                files={"bundle": (bundle.name, body, "application/zip")},
                data=form,
                headers={"X-CSRFToken": csrf_token, "Referer": self.base_url},
            )

    def _csrf_token(self) -> str:
        return str(self.request("GET", "/api/v1/security/csrf_token/")["result"])

    def grant_permissions(self, role: str, permission: str, view_menus: Sequence[str]) -> bool:
        """Add `permission` on each view menu to `role`, keeping what the role already holds.

        Superset's role-permission endpoint replaces the whole set, so the
        grant is the union with the current set: repeating it changes nothing,
        and a partial import never revokes the schemas of another bundle.
        Returns False, granting nothing, when the role does not exist.
        """
        found = self.request("GET", "/api/v1/security/roles/search/", params={"q": _filter("name", role)})
        role_id = next((r["id"] for r in found.get("result", []) if r["name"] == role), None)
        if role_id is None:
            return False
        # permissions-resources defines no searchable columns (a filter is an HTTP 400),
        # so the pages are walked and matched here.
        found_ids: dict[str, int] = {}
        page = 0
        while True:
            body = self.request(
                "GET",
                "/api/v1/security/permissions-resources/",
                params={"q": json.dumps({"page": page, "page_size": 100})},
            )
            rows = body.get("result", [])
            for r in rows:
                if r["permission"]["name"] == permission:
                    found_ids[r["view_menu"]["name"]] = int(r["id"])
            page += 1
            if not rows or page * 100 >= body.get("count", 0):
                break
        missing = [v for v in view_menus if v not in found_ids]
        if missing:
            raise ServiceClientError(f"Superset has no {permission} permission on {missing[0]!r}; was it imported?")
        wanted = {found_ids[v] for v in view_menus}
        held = self.request("GET", f"/api/v1/security/roles/{role_id}/permissions/").get("result", [])
        self.send(
            "POST",
            f"/api/v1/security/roles/{role_id}/permissions",
            json={"permission_view_menu_ids": sorted(wanted | {int(r["id"]) for r in held})},
            headers={"X-CSRFToken": self._csrf_token(), "Referer": self.base_url},
        )
        return True


    def databases(self) -> dict[str, str]:
        """Each database Superset holds: its uuid, canonical, to its name."""
        found: dict[str, str] = {}
        page = 0
        while True:
            body = self.request(
                "GET",
                "/api/v1/database/",
                params={"q": json.dumps({"columns": ["uuid", "database_name"], "page": page, "page_size": 100})},
            )
            rows = body.get("result", [])
            for row in rows:
                found[str(row["uuid"]).lower()] = str(row["database_name"])
            page += 1
            if not rows or page * 100 >= body.get("count", 0):
                return found

    def my_schema_access(self) -> set[str]:
        """The `[database].[schema]` view menus this user's roles hold `schema_access` on."""
        roles = self.request("GET", "/api/v1/me/roles/").get("result", {}).get("roles", {})
        return {str(view) for permissions in roles.values() for name, view in permissions if name == "schema_access"}

    def user_id(self, username: str) -> int | None:
        """The id of the Superset user `username`, or None when there is none."""
        found = self.request("GET", "/api/v1/security/users/", params={"q": _filter("username", username)})
        return next((int(u["id"]) for u in found.get("result", []) if u.get("username") == username), None)

    def add_owner(self, dashboard: str, user_id: int) -> int:
        """Make `user_id` an owner of a dashboard (by uuid or slug), its charts and their datasets.

        Each keeps every owner it has: Superset's PUT replaces the list, so the
        new list is the current one plus this user. Returns how many objects
        gained the owner; repeating it changes nothing.
        """
        ref = quote(dashboard, safe="")
        board = self.request("GET", f"/api/v1/dashboard/{ref}")["result"]
        targets = [("dashboard", int(board["id"]), board.get("owners") or [])]
        for kind, path in (("chart", "charts"), ("dataset", "datasets")):
            for item in self.request("GET", f"/api/v1/dashboard/{ref}/{path}").get("result", []):
                shown = self.request("GET", f"/api/v1/{kind}/{int(item['id'])}")["result"]
                targets.append((kind, int(item["id"]), shown.get("owners") or []))
        changed = 0
        for kind, pk, owners in targets:
            held = [int(owner["id"]) for owner in owners]
            if user_id in held:
                continue
            self.send(
                "PUT",
                f"/api/v1/{kind}/{pk}",
                json={"owners": [*held, user_id]},
                headers={"X-CSRFToken": self._csrf_token(), "Referer": self.base_url},
            )
            changed += 1
        return changed


def _filter(column: str, value: str) -> str:
    return json.dumps({"filters": [{"col": column, "opr": "eq", "value": value}], "page_size": 100})
