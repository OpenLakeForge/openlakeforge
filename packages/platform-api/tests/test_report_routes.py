"""Report import and export against a loopback stand-in for one stage's Superset, as the service user."""

from __future__ import annotations

import email.policy
import io
import json
import threading
import urllib.parse
from collections.abc import Iterator
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zipfile import ZipFile

import pytest
import yaml
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.audit import AuditLog
from openlakeforge_platform_api.settings import Settings

AUDIT_BASE = "s3://ops/audit/platform-api/"
DATABASE = "11111111-1111-5111-8111-111111111111"
DASHBOARD = "22222222-2222-5222-8222-222222222222"
GRANTED = "[Trino].[order_revenue_gold]"
UNREACHABLE = "http://127.0.0.1:9"


class FakeSuperset(ThreadingHTTPServer):
    """Login as olf-platform-api/pw, the database list, the user's own roles, the assets import and export."""

    def __init__(self) -> None:
        self.imports: list[tuple[bytes, dict[str, str]]] = []
        self.schema_access = [GRANTED]
        self.exports = {DASHBOARD: _zip({"dashboard_export/metadata.yaml": "version: 1.0.0\ntype: Dashboard\n"})}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def reply(self, status: int, body: object, content_type: str = "application/json") -> None:
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                if self.path.startswith("/api/v1/security/csrf_token/"):
                    self.send_header("Set-Cookie", "session=s; Path=/")
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # noqa: N802 - http.server's handler name
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/v1/security/login":
                    login = json.loads(body)
                    if (login["username"], login["password"], login["provider"]) != ("olf-platform-api", "pw", "db"):
                        return self.reply(401, {"message": "Not authorized"})
                    return self.reply(200, {"access_token": "t"})
                if self.path == "/api/v1/assets/import/" and self.headers.get("X-CSRFToken") == "c":
                    message = BytesParser(policy=email.policy.default).parsebytes(
                        f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
                    )
                    parts = {p.get_param("name", header="content-disposition"): p for p in message.iter_parts()}
                    form = {str(k): v.get_content().strip() for k, v in parts.items() if k != "bundle"}
                    payload = parts["bundle"].get_payload(decode=True)
                    assert isinstance(payload, bytes)
                    server.imports.append((payload, form))
                    return self.reply(200, {"message": "OK"})
                self.reply(404, {"message": "Not found"})

            def do_GET(self) -> None:  # noqa: N802
                if self.headers.get("Authorization") != "Bearer t":
                    return self.reply(401, {"msg": "Missing Authorization Header"})
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/api/v1/security/csrf_token/":
                    return self.reply(200, {"result": "c"})
                if url.path == "/api/v1/database/":
                    return self.reply(200, {"result": [{"uuid": DATABASE, "database_name": "Trino"}], "count": 1})
                if url.path == "/api/v1/me/roles/":
                    perms = [["schema_access", view] for view in server.schema_access]
                    return self.reply(200, {"result": {"roles": {"olf_platform_api": perms}}})
                if url.path == "/api/v1/dashboard/export/":
                    return self.reply(200, server.exports[DASHBOARD], "application/zip")
                if url.path.startswith("/api/v1/dashboard/"):
                    ref = urllib.parse.unquote(url.path.removeprefix("/api/v1/dashboard/"))
                    if ref not in server.exports:
                        return self.reply(404, {"message": "Not found"})
                    return self.reply(200, {"result": {"id": 4}})
                self.reply(404, {"message": "Not found"})

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()


class AuditStore:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict[str, Any]:  # noqa: N803
        self.records.append(json.loads(Body))
        return {}


def _zip(files: dict[str, str]) -> bytes:
    raw = io.BytesIO()
    with ZipFile(raw, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return raw.getvalue()


def _report(database: str = DATABASE) -> bytes:
    return _zip(
        {
            "metadata.yaml": "version: 1.0.0\ntype: assets\n",
            "databases/trino.yaml": f"database_name: X\nsqlalchemy_uri: trino://x@evil:443/c\nuuid: {database}\n",
            "datasets/mart.yaml": f"table_name: mart\nschema: order_revenue_gold\ndatabase_uuid: {database}\n",
            "dashboards/d.yaml": f"dashboard_title: Orders\nuuid: {DASHBOARD}\n",
        }
    )


@pytest.fixture
def superset() -> Iterator[FakeSuperset]:
    fake = FakeSuperset()
    yield fake
    fake.shutdown()
    fake.server_close()


@pytest.fixture
def store() -> AuditStore:
    return AuditStore()


def _api(settings: Settings, store: AuditStore, url: str) -> TestClient:
    raw = {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": list(settings.stages),
        "superset_urls": {"dev": url, "prod": url},
        "superset_username": "olf-platform-api",
        "audit_base_uri": AUDIT_BASE,
    }
    audit = AuditLog(store, AUDIT_BASE, emit=lambda _line: None)
    return TestClient(create_app(Settings.from_mapping(raw), audit=audit))


@pytest.fixture
def api(settings: Settings, store: AuditStore, superset: FakeSuperset, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPENLAKEFORGE_SUPERSET_PASSWORD_DEV", "pw")
    monkeypatch.setenv("OPENLAKEFORGE_SUPERSET_PASSWORD_PROD", "pw")
    return _api(settings, store, superset.url)


def _as(mint: Mint, role: str) -> dict[str, str]:
    return bearer(mint(groups=list(ROLE_MAPPING[role]), sub=f"{role}-1", email=f"{role}@example.com"))


def _import(api: TestClient, headers: dict[str, str], body: bytes, stage: str = "dev") -> Any:
    return api.post(
        f"/api/v1/stages/{stage}/reports", content=body, headers={**headers, "Content-Type": "application/zip"}
    )


@pytest.mark.parametrize("role", ["platform-admin", "data-engineer"])
def test_a_permitted_import_is_sparse_carries_no_database_and_is_audited(
    api: TestClient, mint: Mint, superset: FakeSuperset, store: AuditStore, role: str
) -> None:
    response = _import(api, _as(mint, role), _report())

    assert response.status_code == 200, response.text
    assert response.json()["dashboards"] == [DASHBOARD]
    [(bundle, form)] = superset.imports
    assert form == {"sparse": "true"}
    with ZipFile(io.BytesIO(bundle)) as archive:
        assert not [name for name in archive.namelist() if "/databases/" in name]
    assert [r["phase"] for r in store.records] == ["attempted", "succeeded"]
    assert store.records[1]["result"] == {"dashboards": DASHBOARD}


@pytest.mark.parametrize(("role", "stage"), [("analyst", "dev"), ("viewer", "dev"), ("platform-admin", "prod")])
def test_a_role_below_the_floor_or_a_stage_other_than_dev_is_refused_and_audited(
    api: TestClient, mint: Mint, superset: FakeSuperset, store: AuditStore, role: str, stage: str
) -> None:
    response = _import(api, _as(mint, role), _report(), stage=stage)

    assert response.status_code == 403
    assert superset.imports == []
    assert [r["phase"] for r in store.records] == ["refused"]


def test_a_report_reading_a_schema_no_role_was_granted_is_409_and_not_imported(
    api: TestClient, mint: Mint, superset: FakeSuperset, store: AuditStore
) -> None:
    superset.schema_access = []

    response = _import(api, _as(mint, "data-engineer"), _report())

    assert response.status_code == 409
    assert GRANTED in response.json()["detail"]
    assert superset.imports == []
    assert [(r["phase"], r["status"]) for r in store.records] == [("attempted", None), ("failed", 409)]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_report("33333333-3333-5333-8333-333333333333"), "uses a database this stage's Superset does not have"),
        (b"not a zip", "not a zip archive"),
    ],
)
def test_an_upload_the_api_will_not_import_is_422_with_its_reason(
    api: TestClient, mint: Mint, superset: FakeSuperset, body: bytes, message: str
) -> None:
    response = _import(api, _as(mint, "data-engineer"), body)

    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert superset.imports == []


def test_an_upload_over_the_limit_is_413_before_anything_is_recorded(
    api: TestClient, mint: Mint, store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openlakeforge_platform_api import report_routes

    monkeypatch.setattr(report_routes, "MAX_UPLOAD_BYTES", 10)

    response = _import(api, _as(mint, "data-engineer"), _report())

    assert response.status_code == 413
    assert store.records == []


def test_an_unreachable_superset_is_502_naming_only_the_stage(
    settings: Settings, store: AuditStore, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_SUPERSET_PASSWORD_DEV", "pw")

    response = _import(_api(settings, store, UNREACHABLE), _as(mint, "data-engineer"), _report())

    assert response.status_code == 502
    assert response.json()["detail"] == "Superset for stage 'dev' is unreachable"


def test_without_the_service_users_password_the_stage_is_503(
    settings: Settings, store: AuditStore, superset: FakeSuperset, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_SUPERSET_PASSWORD_DEV", raising=False)

    response = _import(_api(settings, store, superset.url), _as(mint, "data-engineer"), _report())

    assert response.status_code == 503
    assert superset.imports == []


def test_an_export_is_a_stage_neutral_assets_bundle_on_any_stage(
    api: TestClient, mint: Mint, superset: FakeSuperset
) -> None:
    response = api.get(f"/api/v1/stages/prod/reports/{DASHBOARD}/export", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert yaml.safe_load(archive.read("dashboard_export/metadata.yaml"))["type"] == "assets"


def test_an_export_below_the_floor_is_403_and_an_unknown_dashboard_404(api: TestClient, mint: Mint) -> None:
    assert api.get(f"/api/v1/stages/dev/reports/{DASHBOARD}/export", headers=_as(mint, "analyst")).status_code == 403
    missing = api.get("/api/v1/stages/dev/reports/renamed-away/export", headers=_as(mint, "data-engineer"))
    assert missing.status_code == 404
