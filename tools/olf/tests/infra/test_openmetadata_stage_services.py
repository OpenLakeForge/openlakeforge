"""The per-stage OpenMetadata services against a fake server that upserts, looks up and deletes by name."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra import k8s
from olf.infra.openmetadata import _bootstrap, _stage_services, _stale_services
from olf.infra.openmetadata._stage_services import StageServices

PIPELINES = "/api/v1/services/ingestionPipelines"
DEV = StageServices("dev", "http://dagster.olf-dev:3000", "http://superset.olf-dev:8088", "olf-dev")
PROD = StageServices("prod", "http://dagster.olf-prod:3000")  # governed, no analytics


class FakeServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.entities: dict[str, dict] = {}  # "<collection>/name/<fqn>" -> body with its id
        self.calls: list[tuple[str, str]] = []
        self.fail: tuple[str, str, int] | None = None  # (method, path prefix, status) answered instead


class _Handler(BaseHTTPRequestHandler):
    server: FakeServer

    def log_message(self, *_args: object) -> None:
        pass

    def _reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self) -> None:
        srv = self.server
        srv.calls.append((self.command, self.path))
        path = urlsplit(self.path).path
        if srv.fail and srv.fail[0] == self.command and path.startswith(srv.fail[1]):
            return self._reply(srv.fail[2], {"message": "refused"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.command == "GET":
            entity = srv.entities.get(path)
            return self._reply(200, entity) if entity else self._reply(404, {})
        if self.command == "DELETE":
            collection, entity_id = path.rsplit("/", 1)
            key = next((k for k, v in srv.entities.items() if v["id"] == entity_id and k.startswith(collection)), None)
            if key is None:
                return self._reply(404, {})
            del srv.entities[key]
            return self._reply(200, {})
        if path.startswith(f"{PIPELINES}/deploy/"):
            return self._reply(200, {})
        if path == PIPELINES:  # POST: the pipeline's FQN is its service's name and its own
            service = next(v["name"] for v in srv.entities.values() if v["id"] == body["service"]["id"])
            key = f"{PIPELINES}/name/{service}.{body['name']}"
        else:  # PUT upsert of a service
            key = f"{path}/name/{body['name']}"
        existing = srv.entities.get(key)
        srv.entities[key] = {**body, "id": existing["id"] if existing else f"id-{len(srv.calls)}"}
        self._reply(200 if existing else 201, srv.entities[key])

    do_GET = do_PUT = do_POST = do_DELETE = _handle  # noqa: N815 - BaseHTTPRequestHandler API


@pytest.fixture
def om() -> Iterator[FakeServer]:
    server = FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, object]]:
    """The Superset admin Secret reads: (name, key, namespace, environ)."""
    calls: list[tuple[str, str, str, object]] = []

    def secret_value(name: str, key: str, namespace: str, *, environ: object = None, **_kwargs: object) -> str:
        calls.append((name, key, namespace, environ))
        if namespace != "olf-dev":
            raise k8s.KubectlError(f'secrets "{name}" not found')
        return "superset-secret"

    monkeypatch.setattr(k8s, "secret_value", secret_value)
    return calls


def _writes(om: FakeServer) -> list[tuple[str, str]]:
    return [call for call in om.calls if call[0] != "GET"]


def test_registers_each_stage_service_and_its_paused_pipeline(om: FakeServer, reads: list) -> None:
    environ = {"KUBECONFIG": "/contract/kubeconfig"}

    _stage_services.bootstrap_stage_services(OpenMetadataClient(om.url), [DEV, PROD], environ=environ)

    assert reads == [("superset-credentials", "admin-password", "olf-dev", environ)]
    dagster = om.entities["/api/v1/services/pipelineServices/name/dagster_dev"]
    assert {k: dagster[k] for k in ("displayName", "serviceType", "connection")} == {
        "displayName": "Dagster Orchestrator (dev)",
        "serviceType": "Dagster",
        "connection": {"config": {"type": "Dagster", "host": "http://dagster.olf-dev:3000", "timeout": 1000}},
    }
    superset = om.entities["/api/v1/services/dashboardServices/name/superset_dev"]
    assert superset["displayName"] == "Superset Reporting (dev)"
    assert superset["connection"]["config"] == {
        "type": "Superset",
        "hostPort": "http://superset.olf-dev:8088",
        "connection": {"provider": "db", "username": "admin", "password": "superset-secret", "verifySSL": "no-ssl"},
    }
    pipeline = om.entities[f"{PIPELINES}/name/superset_dev.superset-metadata-ingestion"]
    assert pipeline["sourceConfig"] == {"config": {"type": "DashboardMetadata", "includeDataModels": True}}
    assert pipeline["service"] == {"id": superset["id"], "type": "dashboardService"}
    lineage = om.entities[f"{PIPELINES}/name/dagster_prod.dagster-metadata-ingestion"]
    assert lineage["sourceConfig"] == {"config": {"type": "PipelineMetadata", "includeLineage": True}}
    pipelines = [v for k, v in om.entities.items() if k.startswith(f"{PIPELINES}/name/")]
    assert len(pipelines) == 3
    assert all(p["airflowConfig"]["pausePipeline"] is True and p["pipelineType"] == "metadata" for p in pipelines)
    assert "/api/v1/services/dashboardServices/name/superset_prod" not in om.entities
    assert _writes(om)[:3] == [
        ("PUT", "/api/v1/services/pipelineServices"),
        ("POST", PIPELINES),
        ("POST", f"{PIPELINES}/deploy/{om.entities[f'{PIPELINES}/name/dagster_dev.dagster-metadata-ingestion']['id']}"),
    ]


def test_a_rerun_updates_the_services_and_never_recreates_a_pipeline(om: FakeServer, reads: list) -> None:
    client = OpenMetadataClient(om.url)
    _stage_services.bootstrap_stage_services(client, [DEV, PROD])
    entities = json.loads(json.dumps(om.entities))
    om.calls.clear()

    _stage_services.bootstrap_stage_services(client, [DEV, PROD])

    assert om.entities == entities
    assert [method for method, path in _writes(om) if "/deploy/" not in path] == ["PUT"] * 3
    assert not [call for call in om.calls if call[0] == "DELETE"]


def _id(om: FakeServer, key: str) -> str:
    return om.entities[key]["id"]


def test_services_of_a_stage_without_governance_or_analytics_are_removed(om: FakeServer, reads: list) -> None:
    client = OpenMetadataClient(om.url)
    _stage_services.bootstrap_stage_services(client, [DEV, StageServices("prod", PROD.dagster_url, "u", "olf-dev")])
    ids = [
        _id(om, f"{PIPELINES}/name/superset_dev.superset-metadata-ingestion"),
        _id(om, "/api/v1/services/dashboardServices/name/superset_dev"),
        _id(om, f"{PIPELINES}/name/dagster_prod.dagster-metadata-ingestion"),
        _id(om, "/api/v1/services/pipelineServices/name/dagster_prod"),
        _id(om, f"{PIPELINES}/name/superset_prod.superset-metadata-ingestion"),
        _id(om, "/api/v1/services/dashboardServices/name/superset_prod"),
    ]
    om.calls.clear()

    # dev lost analytics, prod lost governance, uat never had either
    _stage_services.bootstrap_stage_services(client, [StageServices("dev", DEV.dagster_url)])

    assert sorted(om.entities) == [
        f"{PIPELINES}/name/dagster_dev.dagster-metadata-ingestion",
        "/api/v1/services/pipelineServices/name/dagster_dev",
    ]
    assert [call for call in om.calls if call[0] == "DELETE"] == [
        ("DELETE", f"{PIPELINES}/{ids[0]}?hardDelete=true"),
        ("DELETE", f"/api/v1/services/dashboardServices/{ids[1]}?hardDelete=true&recursive=true"),
        ("DELETE", f"{PIPELINES}/{ids[2]}?hardDelete=true"),
        ("DELETE", f"/api/v1/services/pipelineServices/{ids[3]}?hardDelete=true&recursive=true"),
        ("DELETE", f"{PIPELINES}/{ids[4]}?hardDelete=true"),
        ("DELETE", f"/api/v1/services/dashboardServices/{ids[5]}?hardDelete=true&recursive=true"),
    ]


@pytest.mark.parametrize(
    ("method", "prefix"),
    [
        ("PUT", "/api/v1/services/pipelineServices"),
        ("GET", f"{PIPELINES}/name/"),
        ("POST", PIPELINES),
        ("DELETE", "/api/v1/services/"),
    ],
)
def test_a_refused_call_fails_the_bootstrap(om: FakeServer, reads: list, method: str, prefix: str) -> None:
    om.entities["/api/v1/services/dashboardServices/name/superset_uat"] = {"id": "s9", "name": "superset_uat"}
    om.fail = (method, prefix, 403)

    with pytest.raises(OpenMetadataError, match="403"):
        _stage_services.bootstrap_stage_services(OpenMetadataClient(om.url), [DEV])


def test_a_refused_deploy_only_warns(om: FakeServer, reads: list) -> None:
    om.fail = ("POST", f"{PIPELINES}/deploy/", 500)

    _stage_services.bootstrap_stage_services(OpenMetadataClient(om.url), [DEV])

    assert f"{PIPELINES}/name/superset_dev.superset-metadata-ingestion" in om.entities


def test_a_missing_superset_secret_fails_before_the_dashboard_service(om: FakeServer, reads: list) -> None:
    with pytest.raises(k8s.KubectlError, match="not found"):
        _stage_services.bootstrap_stage_services(
            OpenMetadataClient(om.url), [StageServices("dev", DEV.dagster_url, DEV.superset_url, "olf-elsewhere")]
        )

    assert "/api/v1/services/dashboardServices/name/superset_dev" not in om.entities


def _invoke(monkeypatch: pytest.MonkeyPatch, *flags: str) -> tuple[int, list[tuple[object, list, object]]]:
    from typer.testing import CliRunner

    from olf.cli import app
    from olf.infra.commands import runtime

    contract = {"OPENLAKEFORGE_SHARED_NAMESPACE": "olf-shared"}
    registered: list[tuple[object, list, object]] = []
    monkeypatch.setattr(runtime, "provider_contract_environment", lambda **_kwargs: contextlib.nullcontext(contract))
    monkeypatch.setattr(_bootstrap, "bootstrap_auth", lambda *_args, **_kwargs: "client")
    monkeypatch.setattr(_stale_services, "remove_legacy_services", lambda _client: None)
    monkeypatch.setattr(
        _stage_services,
        "bootstrap_stage_services",
        lambda c, stages, environ: registered.append((c, stages, environ)),
    )
    result = CliRunner().invoke(app, ["infra", "openmetadata", "bootstrap", "--om-url", "http://om:8585", *flags])
    return result.exit_code, registered


def test_the_command_registers_stage_services_from_its_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    code, registered = _invoke(
        monkeypatch,
        *("--dagster-url", "dev=http://dagster.olf-dev:3000", "--dagster-url", "prod=http://dagster.olf-prod:3000"),
        *("--superset-url", "dev=http://superset.olf-dev:8088", "--superset-namespace", "dev=olf-dev"),
    )

    assert code == 0
    assert registered == [("client", [DEV, PROD], {"OPENLAKEFORGE_SHARED_NAMESPACE": "olf-shared"})]


def test_without_dagster_urls_the_command_leaves_stage_services_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    code, registered = _invoke(monkeypatch)

    assert code == 0
    assert registered == []


@pytest.mark.parametrize(
    "flags",
    [
        ("--dagster-url", "qa=http://d"),
        ("--dagster-url", "dev"),
        ("--dagster-url", "dev=http://a", "--dagster-url", "dev=http://b"),
        ("--dagster-url", "dev=http://d", "--superset-url", "prod=http://s", "--superset-namespace", "prod=ns"),
        ("--dagster-url", "dev=http://d", "--superset-url", "dev=http://s"),
    ],
)
def test_the_command_rejects_malformed_stage_flags(monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]) -> None:
    code, registered = _invoke(monkeypatch, *flags)

    assert code != 0
    assert registered == []
