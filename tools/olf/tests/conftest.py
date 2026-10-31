"""Shared test fixtures used across the olf.infra.e2e test files.

`e2e_cfg`/`E2E_REPO_ROOT`/`E2E_INVENTORY` are used by most of the
`test_e2e_*.py` files (mirroring `olf/infra/e2e/`'s capability submodules), so they
live here once rather than duplicated per file.
"""

from __future__ import annotations

import email.policy
import json
import re
import shutil
import threading
import urllib.parse
from collections.abc import Iterator, Sequence
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from openlakeforge_domain import inventory_for

from olf.common.profile import StageName
from olf.infra.e2e._shell import E2EConfig, Environment, Suite


@pytest.fixture(autouse=True)
def _isolate_toolchain(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Every test defaults to host-mode executable resolution with an
    isolated `OLF_HOME`.

    `olf.common.tooling.resolver.build_resolver()` provisions managed tools (#127)
    over the network into `OLF_HOME` (`~/.openlakeforge` by default) unless
    told otherwise. Without this guard, any test that exercises a real
    (unmocked) `kubectl`/`terraform` execution path - directly or via
    `olf.infra.k8s`/`olf.infra.e2e._shell` - would silently download real Terraform/
    kubectl/helm/kind binaries onto the network and into the developer's
    actual home directory. Tests that specifically exercise managed-mode
    resolution (`tests/test_toolchain_*.py`) override both variables
    themselves with an injected fake downloader.
    """
    monkeypatch.setenv("OLF_TOOLCHAIN_MODE", "host")
    monkeypatch.setenv("OLF_HOME", str(tmp_path_factory.mktemp("olf-home")))

E2E_REPO_ROOT = Path(__file__).resolve().parents[3]
E2E_INVENTORY = inventory_for(E2E_REPO_ROOT)
CONFORMANCE_PROFILE = E2E_REPO_ROOT / "openlakeforge.conformance.yaml"
CONFORMANCE_STAGES = (StageName.DEV, StageName.PROD)


@pytest.fixture(autouse=True)
def _pin_project_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test resolves the project and distribution roots from this
    checkout instead of from the process working directory.

    `olf.common.config.repo_root()` defaults to `"."`, and `project_root()` and
    `distribution_root()` both fall back to it. A real run never reaches that
    default because `provider_contract_environment` exports the variable
    first; a test that stubs out the hydration does reach it, and then
    resolves whichever directory pytest happened to be started from - which
    is why nine CLI tests passed from the repository root and failed from
    `tools/olf` (#190). `OLF_DISTRIBUTION_ROOT` and `OPENLAKEFORGE_PROJECT_ROOT`
    both outrank `OPENLAKEFORGE_REPO_ROOT` in `config.py`, so an ambient
    export of either would shadow the pin below and reintroduce #190 - they
    are deleted, not pinned, because pinning `OLF_DISTRIBUTION_ROOT` globally
    would itself shadow the project root a test selects for itself. A test
    that needs one sets it locally afterwards, and monkeypatch tears down
    fixtures in reverse order, so the test's own value still wins.
    """
    monkeypatch.delenv("OLF_DISTRIBUTION_ROOT", raising=False)
    monkeypatch.delenv("OPENLAKEFORGE_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(E2E_REPO_ROOT))


@pytest.fixture(autouse=True)
def _query_sizes_in_stub_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contexts built over an empty `tmp_path` still find `release/query-sizes.yaml`.

    `query_variables` reads the size mapping from the distribution root, and
    most deployment tests build their context around a bare temporary
    directory rather than a payload. A test that supplies its own file wins.
    """
    from olf.common.deployment import context
    from olf.common.query_sizing import QUERY_SIZES_PATH, load_query_sizing

    def load(distribution_root: Path, size):  # noqa: ANN001, ANN202
        present = (distribution_root / QUERY_SIZES_PATH).is_file()
        return load_query_sizing(distribution_root if present else E2E_REPO_ROOT, size)

    monkeypatch.setattr(context, "load_query_sizing", load)


@pytest.fixture
def external_project(tmp_path: Path) -> Path:
    """Copy only the versioned data-project payload into a separate root."""
    root = tmp_path / "external-project"
    root.mkdir()
    shutil.copy2(E2E_REPO_ROOT / "openlakeforge.yaml", root / "openlakeforge.yaml")
    shutil.copytree(E2E_REPO_ROOT / "lakehouse_code", root / "lakehouse_code")
    return root


def e2e_cfg(tmp_path: Path, env: Environment = "local", suite: Suite = "full") -> E2EConfig:
    return E2EConfig(
        env=env,
        suite=suite,
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=E2E_INVENTORY,
        aws_region="eu-west-1" if env == "aws" else None,
    )


def write_floe_entity(lakehouse_code: Path, domain: str, name: str, source: str, resource: str | None = None) -> None:
    """A domain's Floe entity file; the Silver table list is read from these."""
    entity_dir = lakehouse_code / "silver" / domain / "contracts" / "floe"
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / f"{name}.yml").write_text(
        f'name: "{name}"\nsource: {{ ref: "{source}", resource: "{resource or name}" }}\n', encoding="utf-8"
    )


def write_two_product_fixture(root: Path, *, dashboards: tuple[tuple[str, str], ...] = ()) -> None:
    """A minimal two-product, single-domain governance descriptor for isolation
    tests. ``dashboards`` is a sequence of ``(dashboard_name, product_id)``
    pairs; each becomes a report bundle (a ``metadata.yaml``) serving that
    product. The caller still has to write the actual Superset export via
    ``write_dashboard_fixture``.
    """
    lakehouse_dir = root / "lakehouse_code"
    source_dir = lakehouse_dir / "bronze" / "widgets_source"
    source_dir.mkdir(parents=True)
    (source_dir / "source.yaml").write_text(
        """\
apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: widgets_source
displayName: Widgets Source
description: Widgets bronze source fixture.
status: planned
resources:
  - name: source
""",
        encoding="utf-8",
    )
    for name, product in dashboards:
        bundle = lakehouse_dir / "dashboards" / "superset" / name
        (bundle / "datasets" / "Trino").mkdir(parents=True)
        (bundle / "metadata.yaml").write_text("type: assets\n", encoding="utf-8")
        (bundle / "datasets" / "Trino" / "mart.yaml").write_text(f"schema: {product}_gold\n", encoding="utf-8")
    (lakehouse_dir / "governance.yml").write_text(
        """\
apiVersion: openlakeforge.io/v1beta1
kind: Governance
domains:
  - name: widgets
    displayName: Widgets
    description: Widgets domain fixture.
    status: planned
products:
  - id: widgets_alpha
    domain: widgets
    displayName: Widgets Alpha
    description: Alpha product.
    status: planned
  - id: widgets_beta
    domain: widgets
    displayName: Widgets Beta
    description: Beta product.
    status: planned
""",
        encoding="utf-8",
    )


def write_dashboard_fixture(repo_root: Path, report_source_dir: str, file_name: str, *, slug: str, title: str) -> None:
    report_dir = repo_root / report_source_dir
    dashboards_dir = report_dir / "dashboards"
    dashboards_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "metadata.yaml").write_text("type: assets\n", encoding="utf-8")
    (dashboards_dir / file_name).write_text(f"dashboard_title: {title}\nslug: {slug}\n", encoding="utf-8")


class FakeSuperset(ThreadingHTTPServer):
    """A Superset stand-in serving the REST endpoints olf uses, with their auth rules.

    Login takes `admin`/`secret`. Every other endpoint needs the bearer token,
    and the import additionally needs the CSRF token in `X-CSRFToken` plus the
    session cookie `csrf_token/` set, as Flask-WTF enforces.
    """

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeSupersetHandler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.login_failures = 0
        self.login_attempts = 0
        self.dashboards: dict[str, int] = {}  # uuid or slug -> id
        self.exports: dict[int, bytes] = {}  # id -> export zip
        self.imported: list[bytes] = []
        self.roles: dict[str, set[int]] = {"olf_data_access": set()}  # role name -> permission-view-menu ids
        self.permissions: dict[int, tuple[str, str]] = {}  # id -> (permission, view menu)


class _FakeSupersetHandler(BaseHTTPRequestHandler):
    server: FakeSuperset

    def log_message(self, *_args: object) -> None:
        pass

    def _reply(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == "Bearer fake-token"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/api/v1/security/login":
            self.server.login_attempts += 1
            if self.server.login_failures:
                self.server.login_failures -= 1
                return self._reply(503, {"message": "starting"})
            credentials = json.loads(body)
            if (credentials["username"], credentials["password"]) != ("admin", "secret"):
                return self._reply(401, {"message": "Not authorized"})
            return self._reply(200, {"access_token": "fake-token"})
        if self.path == "/api/v1/assets/import/":
            if not self._authorized():
                return self._reply(401, {"msg": "Missing Authorization Header"})
            if self.headers.get("X-CSRFToken") != "csrf-1" or "session=csrf-session" not in self.headers.get(
                "Cookie", ""
            ):
                return self._reply(400, {"errors": "The CSRF token is missing."})
            message = BytesParser(policy=email.policy.default).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
            )
            bundles = [
                part
                for part in message.iter_parts()
                if part.get_param("name", header="content-disposition") == "bundle"
            ]
            if not bundles:
                return self._reply(400, {"message": "Request is not valid"})
            self.server.imported.append(bundles[0].get_payload(decode=True))
            return self._reply(200, {"message": "OK"})
        role_grant = re.fullmatch(r"/api/v1/security/roles/(\d+)/permissions", self.path)
        if role_grant and self._authorized():
            if self.headers.get("X-CSRFToken") != "csrf-1":
                return self._reply(400, {"errors": "The CSRF token is missing."})
            name = sorted(self.server.roles)[int(role_grant[1])]
            self.server.roles[name] = set(json.loads(body)["permission_view_menu_ids"])
            return self._reply(200, {"result": "ok"})
        self._reply(404, {"message": "Not found"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        url = urllib.parse.urlsplit(self.path)
        if not self._authorized():
            return self._reply(401, {"msg": "Missing Authorization Header"})
        if url.path == "/api/v1/security/csrf_token/":
            return self._reply(200, {"result": "csrf-1"}, {"Set-Cookie": "session=csrf-session; Path=/"})
        query = (
            json.loads(urllib.parse.parse_qs(url.query).get("q", ["{}"])[0])
            if url.path != "/api/v1/dashboard/export/"
            else {}
        )
        filters = query.get("filters", [{}])[0]
        if url.path == "/api/v1/security/roles/search/":
            names = sorted(self.server.roles)
            return self._reply(
                200, {"result": [{"id": names.index(n), "name": n} for n in names if n == filters["value"]]}
            )
        if url.path == "/api/v1/security/permissions-resources/":
            if "filters" in query:  # the real endpoint has no searchable columns
                return self._reply(400, {"message": "Filter column not allowed"})
            rows = [
                {"id": i, "permission": {"name": p}, "view_menu": {"name": v}}
                for i, (p, v) in sorted(self.server.permissions.items())
            ]
            size = query["page_size"]
            page = rows[query["page"] * size : (query["page"] + 1) * size]
            return self._reply(200, {"result": page, "count": len(rows)})
        role_perms = re.fullmatch(r"/api/v1/security/roles/(\d+)/permissions/", url.path)
        if role_perms:
            held = self.server.roles[sorted(self.server.roles)[int(role_perms[1])]]
            return self._reply(200, {"result": [{"id": i} for i in sorted(held)]})
        if url.path == "/api/v1/dashboard/export/":
            (query,) = urllib.parse.parse_qs(url.query)["q"]
            dashboard_id = int(query.removeprefix("!(").removesuffix(")"))
            if dashboard_id not in self.server.exports:
                return self._reply(404, {"message": "Not found"})
            return self._reply(200, self.server.exports[dashboard_id], {"Content-Type": "application/zip"})
        if url.path == "/api/v1/dashboard/":
            return self._reply(200, {"result": [{"id": i, "slug": ref} for ref, i in self.server.dashboards.items()]})
        if url.path.startswith("/api/v1/dashboard/"):
            ref = urllib.parse.unquote(url.path.removeprefix("/api/v1/dashboard/"))
            if ref not in self.server.dashboards:
                return self._reply(404, {"message": "Not found"})
            return self._reply(200, {"id": self.server.dashboards[ref], "result": {"id": self.server.dashboards[ref]}})
        self._reply(404, {"message": "Not found"})


class FakeOpenMetadata(ThreadingHTTPServer):
    """An OpenMetadata stand-in serving cursor-paged domains and data products to the bot token.

    Every request is recorded as (method, path), so a test can prove a command
    only reads. `fail_status` makes the collection endpoints answer with that status.
    """

    token = "bot-jwt"

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeOpenMetadataHandler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.domains: list[dict[str, str]] = []
        self.data_products: list[dict[str, object]] = []
        self.queries: list[dict[str, list[str]]] = []
        self.requests: list[tuple[str, str]] = []
        self.fail_status: int | None = None


class _FakeOpenMetadataHandler(BaseHTTPRequestHandler):
    server: FakeOpenMetadata

    def log_message(self, *_args: object) -> None:
        pass

    def _reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self) -> urllib.parse.SplitResult:
        url = urllib.parse.urlsplit(self.path)
        self.server.requests.append((self.command, url.path))
        return url

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        url = self._record()
        if self.headers.get("Authorization") != f"Bearer {self.server.token}":
            return self._reply(401, {"message": "Not authorized"})
        collections: dict[str, Sequence[object]] = {
            "/api/v1/domains": self.server.domains,
            "/api/v1/dataProducts": self.server.data_products,
        }
        if url.path not in collections:
            return self._reply(404, {"message": "Not found"})
        if self.server.fail_status:
            return self._reply(self.server.fail_status, {"message": "collection unavailable"})
        query = urllib.parse.parse_qs(url.query)
        self.server.queries.append(query)
        entities = collections[url.path]
        start = int(query.get("after", ["0"])[0])
        end = start + int(query["limit"][0])
        paging: dict[str, object] = {"total": len(entities)}
        if end < len(entities):
            paging["after"] = str(end)
        self._reply(200, {"data": entities[start:end], "paging": paging})

    def _refuse(self) -> None:
        self._record()
        self._reply(405, {"message": "Method not allowed"})

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815 - BaseHTTPRequestHandler API


@pytest.fixture
def fake_openmetadata() -> Iterator[FakeOpenMetadata]:
    server = FakeOpenMetadata()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fake_superset() -> Iterator[FakeSuperset]:
    server = FakeSuperset()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
