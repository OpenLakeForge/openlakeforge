"""The Polaris bootstrap's person-access part (ADR 0019) leaves exactly the grants data_access names."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[4] / "infra/terraform/modules/catalog/polaris/templates"
CATALOG_ROLES = {
    "olf-write": ["CATALOG_MANAGE_CONTENT"],
    "olf-read": ["TABLE_READ_DATA", "NAMESPACE_READ_PROPERTIES", "VIEW_READ_PROPERTIES"],
    "olf-gold-read": ["NAMESPACE_LIST"],
}
# What person_access.tf renders for release/identity-roles.yaml with DEV and PROD enabled.
ASSIGNED = {
    "olf-signed-in": {},
    "olf-platform-admin": {"lakehouse_dev": "olf-write", "lakehouse_prod": "olf-write"},
    "olf-data-engineer": {"lakehouse_dev": "olf-write", "lakehouse_prod": "olf-read"},
    "olf-analyst": {"lakehouse_dev": "olf-gold-read", "lakehouse_prod": "olf-gold-read"},
    "olf-viewer": {"lakehouse_prod": "olf-gold-read"},
}
PERSON_ACCESS = {
    "principal": "github",
    "catalog_roles": CATALOG_ROLES,
    "catalogs": ["lakehouse_dev", "lakehouse_prod"],
    "assigned": ASSIGNED,
}


class FakePolaris:
    """The management endpoints the script calls, with Polaris's answers: 409 on a repeated create, 500 on a repeated
    assignment or grant (the bootstrap's already_present exists because of it)."""

    def __init__(self) -> None:
        self.principals: dict[str, list[str]] = {}
        self.principal_roles: dict[str, dict[str, list[str]]] = {}
        self.catalog_roles: dict[str, dict[str, list[str]]] = {"lakehouse_dev": {}, "lakehouse_prod": {}}
        self.writes: list[str] = []

    def handle(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        parts = path.removeprefix("/api/management/v1/").split("/")
        if method != "GET":
            self.writes.append(f"{method} {path}")
        match method, parts:
            case "POST", ["principals"]:
                return self._create(self.principals, body["principal"]["name"], [])
            case "POST", ["principal-roles"]:
                return self._create(self.principal_roles, body["principalRole"]["name"], {})
            case "GET", ["principals", name, "principal-roles"]:
                return 200, {"roles": [{"name": role} for role in self.principals[name]]}
            case "PUT", ["principals", name, "principal-roles"]:
                return self._add(self.principals[name], body["principalRole"]["name"])
            case "POST", ["catalogs", catalog, "catalog-roles"]:
                return self._create(self.catalog_roles[catalog], body["catalogRole"]["name"], [])
            case "GET", ["catalogs", catalog, "catalog-roles", role, "grants"]:
                grants = self.catalog_roles[catalog][role]
                return 200, {"grants": [{"type": "catalog", "privilege": privilege} for privilege in grants]}
            case "PUT", ["catalogs", catalog, "catalog-roles", role, "grants"]:
                return self._add(self.catalog_roles[catalog][role], body["grant"]["privilege"])
            case "GET", ["principal-roles", role, "catalog-roles", catalog]:
                if role not in self.principal_roles:
                    return 404, {}
                return 200, {"roles": [{"name": name} for name in self.principal_roles[role].get(catalog, [])]}
            case "PUT", ["principal-roles", role, "catalog-roles", catalog]:
                return self._add(self.principal_roles[role].setdefault(catalog, []), body["catalogRole"]["name"])
            case "DELETE", ["principal-roles", role, "catalog-roles", catalog, name]:
                self.principal_roles[role][catalog].remove(name)
                return 204, {}
        return 400, {"error": f"unexpected {method} {path}"}

    @staticmethod
    def _create(collection: dict, name: str, empty: object) -> tuple[int, dict]:
        if name in collection:
            return 409, {}
        collection[name] = empty
        return 201, {}

    @staticmethod
    def _add(values: list[str], value: str) -> tuple[int, dict]:
        if value in values:
            return 500, {"error": "grant_records_pkey"}
        values.append(value)
        return 201, {}

    def assignments(self) -> dict[str, dict[str, list[str]]]:
        return {
            role: {catalog: sorted(names) for catalog, names in catalogs.items() if names}
            for role, catalogs in self.principal_roles.items()
        }


@pytest.fixture
def polaris() -> Iterator[tuple[FakePolaris, str]]:
    fake = FakePolaris()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            status, payload = fake.handle(self.command, self.path, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _serve

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _run(url: str, tmp_path: Path, person_access: dict = PERSON_ACCESS) -> subprocess.CompletedProcess[str]:
    # The bootstrap's own request and already_present, as templatefile renders them.
    bootstrap = (TEMPLATES / "bootstrap.sh.tftpl").read_text()
    helpers = "".join(
        re.search(rf"^  {name}\(\) \{{\n.*?^  \}}\n", bootstrap, re.S | re.M).group(0)  # type: ignore[union-attr]
        for name in ("request", "already_present")
    )
    person = (TEMPLATES / "person-access.sh.tftpl").read_text().replace("${person_access}", json.dumps(person_access))
    script = (
        f'set -eu\npolaris_url="{url}"\nPOLARIS_TOKEN=t\n' + helpers.replace("%%{", "%{").replace("$${", "${") + person
    ).replace("/tmp/polaris-body", str(tmp_path / "body"))
    return subprocess.run(["sh", "-ec", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})


def test_a_fresh_catalog_gets_the_principal_its_roles_and_exactly_the_named_assignments(
    polaris: tuple[FakePolaris, str], tmp_path: Path
) -> None:
    fake, url = polaris

    result = _run(url, tmp_path)

    assert result.returncode == 0, result.stderr
    assert sorted(fake.principals["github"]) == sorted(ASSIGNED)
    assert fake.catalog_roles["lakehouse_prod"] == CATALOG_ROLES
    assert fake.assignments() == {
        role: {catalog: [name] for catalog, name in catalogs.items()} for role, catalogs in ASSIGNED.items()
    }


def test_a_rerun_writes_nothing(polaris: tuple[FakePolaris, str], tmp_path: Path) -> None:
    fake, url = polaris
    _run(url, tmp_path)
    fake.writes.clear()

    result = _run(url, tmp_path)

    assert result.returncode == 0, result.stderr
    assert [write for write in fake.writes if not write.startswith("POST")] == []


def test_a_narrowed_row_loses_the_grant_it_no_longer_names(polaris: tuple[FakePolaris, str], tmp_path: Path) -> None:
    fake, url = polaris
    wider = {**ASSIGNED, "olf-analyst": {"lakehouse_dev": "olf-read", "lakehouse_prod": "olf-read"}}
    _run(url, tmp_path, {**PERSON_ACCESS, "assigned": wider})
    fake.principal_roles["olf-signed-in"]["lakehouse_dev"] = ["olf-write"]

    result = _run(url, tmp_path)

    assert result.returncode == 0, result.stderr
    gold = ["olf-gold-read"]
    assert fake.assignments()["olf-analyst"] == {"lakehouse_dev": gold, "lakehouse_prod": gold}
    assert fake.assignments()["olf-signed-in"] == {}
