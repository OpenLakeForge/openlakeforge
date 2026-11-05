"""Removing stale OpenMetadata services against a fake server that looks up by name and deletes by id."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra.openmetadata import _bootstrap, _stale_services

PIPELINES = "/api/v1/services/ingestionPipelines"


class FakeServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.entities: dict[str, str] = {}  # "<collection>/name/<fqn>" -> id
        self.calls: list[tuple[str, str]] = []
        self.fail: tuple[str, int] | None = None  # (method, status) answered instead


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
        if srv.fail and srv.fail[0] == self.command:
            return self._reply(srv.fail[1], {"message": "refused"})
        path = urlsplit(self.path).path
        if self.command == "GET":
            entity_id = srv.entities.get(path)
            return self._reply(200, {"id": entity_id}) if entity_id else self._reply(404, {})
        collection, entity_id = path.rsplit("/", 1)
        key = next((k for k, v in srv.entities.items() if v == entity_id and k.startswith(f"{collection}/name/")), None)
        if key is None:
            return self._reply(404, {})
        del srv.entities[key]
        self._reply(200, {})

    do_GET = do_DELETE = _handle  # noqa: N815 - BaseHTTPRequestHandler API


@pytest.fixture
def om() -> Iterator[FakeServer]:
    server = FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _legacy(om: FakeServer) -> None:
    om.entities.update(
        {
            f"{PIPELINES}/name/dagster.dagster-metadata-ingestion": "p1",
            "/api/v1/services/pipelineServices/name/dagster": "s1",
            f"{PIPELINES}/name/superset.superset-metadata-ingestion": "p2",
            "/api/v1/services/dashboardServices/name/superset": "s2",
            "/api/v1/services/pipelineServices/name/dagster_dev": "s3",  # a stage service is left alone
        }
    )


def test_removes_each_legacy_pipeline_before_its_service(om: FakeServer) -> None:
    _legacy(om)

    _stale_services.remove_legacy_services(OpenMetadataClient(om.url))

    assert om.entities == {"/api/v1/services/pipelineServices/name/dagster_dev": "s3"}
    assert [call for call in om.calls if call[0] == "DELETE"] == [
        ("DELETE", f"{PIPELINES}/p1?hardDelete=true"),
        ("DELETE", "/api/v1/services/pipelineServices/s1?hardDelete=true&recursive=true"),
        ("DELETE", f"{PIPELINES}/p2?hardDelete=true"),
        ("DELETE", "/api/v1/services/dashboardServices/s2?hardDelete=true&recursive=true"),
    ]


def test_nothing_to_remove_issues_no_delete(om: FakeServer) -> None:
    _stale_services.remove_legacy_services(OpenMetadataClient(om.url))

    assert [method for method, _path in om.calls] == ["GET"] * 4


def test_a_service_whose_pipeline_is_already_gone_is_still_removed(om: FakeServer) -> None:
    om.entities["/api/v1/services/dashboardServices/name/superset"] = "s2"

    _stale_services.remove_legacy_services(OpenMetadataClient(om.url))

    assert om.entities == {}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_a_refused_lookup_or_delete_fails_the_bootstrap(om: FakeServer, method: str) -> None:
    _legacy(om)
    om.fail = (method, 403)

    with pytest.raises(OpenMetadataError, match="403"):
        _stale_services.remove_legacy_services(OpenMetadataClient(om.url))

    assert len(om.entities) == 5


def test_the_command_removes_legacy_services_after_the_auth_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from olf.cli import app
    from olf.infra.commands import runtime

    client = object()
    removed: list[object] = []
    monkeypatch.setattr(runtime, "provider_contract_environment", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setenv("OPENLAKEFORGE_SHARED_NAMESPACE", "olf-shared")
    monkeypatch.setattr(_bootstrap, "bootstrap_auth", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(_stale_services, "remove_legacy_services", removed.append)

    result = CliRunner().invoke(app, ["infra", "openmetadata", "bootstrap", "--om-url", "http://om:8585"])

    assert result.exit_code == 0, result.output
    assert removed == [client]
