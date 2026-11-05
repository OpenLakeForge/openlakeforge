"""The OpenMetadata catalog bootstrap against a fake server that upserts by name."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra.openmetadata import _bootstrap, _catalog_seed, _stale_services
from olf.infra.openmetadata._catalog_seed import CatalogSeed


class FakeServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.entities: dict[str, dict] = {}  # "<path>/<name>" -> body, what the upserts left behind
        self.calls: list[tuple[str, str]] = []
        self.reject: str | None = None  # a path answered with 400


class _Handler(BaseHTTPRequestHandler):
    server: FakeServer

    def log_message(self, *_args: object) -> None:
        pass

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv.calls.append(("PUT", self.path))
        name = f"{body['database']}.{body['name']}" if "database" in body else body.get("name", body.get("config_type"))
        key = f"{self.path}/{name}"
        status = 400 if self.path == srv.reject else 200 if key in srv.entities else 201
        srv.entities[key] = body
        payload = b"{}"
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def om() -> Iterator[FakeServer]:
    server = FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


SEED = CatalogSeed(
    service="polaris",
    display_name="Polaris Lakehouse",
    trino_namespace="trino://trino.olf-shared:8080",
    trino_tls_namespace="trino://trino.olf-shared:8443",
    pipeline_service="dagster_dev",
    databases=("lakehouse_dev", "lakehouse_prod"),
    schemas=("silver", "gold"),
)


def test_writes_openlineage_the_trino_service_and_every_stage_database(om: FakeServer) -> None:
    _catalog_seed.bootstrap_catalog(OpenMetadataClient(om.url), SEED)

    settings = om.entities["/api/v1/system/settings/openLineageSettings"]["config_value"]
    assert settings["defaultPipelineService"] == "dagster_dev"
    assert settings["namespaceToServiceMapping"] == {
        "trino://trino.olf-shared:8080": "polaris",
        "polaris": "polaris",
        "trino://trino.olf-shared:8443": "polaris",
    }
    service = om.entities["/api/v1/services/databaseServices/polaris"]
    assert service["displayName"] == "Polaris Lakehouse"
    assert service["connection"]["config"] == {
        "type": "Trino",
        "hostPort": "trino.olf-shared:8080",
        "username": "openmetadata",
    }
    assert om.entities["/api/v1/databases/lakehouse_prod"] == {"name": "lakehouse_prod", "service": "polaris"}
    assert [(path, body["database"]) for path, body in _schemas(om)] == [
        ("silver", "polaris.lakehouse_dev"),
        ("gold", "polaris.lakehouse_dev"),
        ("silver", "polaris.lakehouse_prod"),
        ("gold", "polaris.lakehouse_prod"),
    ]


def _schemas(om: FakeServer) -> list[tuple[str, dict]]:
    return [(b["name"], b) for k, b in om.entities.items() if k.startswith("/api/v1/databaseSchemas/")]


def test_without_a_tls_listener_only_the_plain_namespace_is_mapped(om: FakeServer) -> None:
    seed = CatalogSeed(**{**SEED.__dict__, "trino_tls_namespace": ""})

    _catalog_seed.enable_openlineage(OpenMetadataClient(om.url), seed)

    mapping = om.entities["/api/v1/system/settings/openLineageSettings"]["config_value"]["namespaceToServiceMapping"]
    assert mapping == {"trino://trino.olf-shared:8080": "polaris", "polaris": "polaris"}


def test_a_rerun_upserts_the_same_entities(om: FakeServer) -> None:
    client = OpenMetadataClient(om.url)
    _catalog_seed.bootstrap_catalog(client, SEED)
    entities = dict(om.entities)

    _catalog_seed.bootstrap_catalog(client, SEED)

    assert om.entities == entities
    assert len(om.calls) == 2 * (2 + 2 + 4)


def test_a_refused_upsert_fails_the_bootstrap(om: FakeServer) -> None:
    om.reject = "/api/v1/databases"

    with pytest.raises(OpenMetadataError, match="400"):
        _catalog_seed.bootstrap_catalog(OpenMetadataClient(om.url), SEED)

    assert not _schemas(om)


def test_the_command_seeds_the_catalog_after_the_auth_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from olf.cli import app
    from olf.infra.commands import runtime

    client = object()
    seeded: list[tuple[object, CatalogSeed]] = []
    monkeypatch.setattr(runtime, "provider_contract_environment", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setenv("OPENLAKEFORGE_SHARED_NAMESPACE", "olf-shared")
    monkeypatch.setattr(_bootstrap, "bootstrap_auth", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(_catalog_seed, "bootstrap_catalog", lambda c, seed: seeded.append((c, seed)))
    monkeypatch.setattr(_stale_services, "remove_legacy_services", lambda _client: None)
    argv = ["infra", "openmetadata", "bootstrap", "--om-url", "http://om:8585"]

    incomplete = CliRunner().invoke(app, [*argv, "--catalog-service", "polaris"])
    result = CliRunner().invoke(
        app,
        [*argv, "--catalog-service", "polaris", "--trino-namespace", "trino://t:8080"]
        + ["--pipeline-service", "dagster_dev", "--database", "lakehouse_dev", "--schema", "silver"],
    )

    assert incomplete.exit_code != 0
    assert result.exit_code == 0, result.output
    assert seeded == [  # the incomplete invocation seeded nothing
        (
            client,
            CatalogSeed(
                service="polaris",
                display_name="polaris",
                trino_namespace="trino://t:8080",
                pipeline_service="dagster_dev",
                databases=("lakehouse_dev",),
                schemas=("silver",),
            ),
        )
    ]
