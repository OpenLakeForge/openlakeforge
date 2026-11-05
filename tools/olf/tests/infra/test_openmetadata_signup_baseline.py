"""The SSO signup baseline against a fake server that applies JSON patches."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra.openmetadata import _bootstrap, _signup_baseline, _stale_services

ORG = "/api/v1/teams/name/Organization"
POLICY = "/api/v1/policies/name/OrganizationPolicy"
NO_OWNER = _signup_baseline.NO_OWNER_RULE


class FakeServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.entities: dict[str, dict] = {
            ORG: {"defaultRoles": [{"name": "DataConsumer"}, {"name": "DataSteward"}]},
            POLICY: {"rules": [{"name": NO_OWNER}, {"name": "OrganizationPolicy-Owner-Rule"}, {"name": NO_OWNER}]},
        }
        self.patches: list[tuple[str, str, list]] = []  # (path, content type, ops)
        self.reject_patch = False


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

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        entity = self.server.entities.get(urlsplit(self.path).path)
        self._reply(200 if entity is not None else 404, entity or {})

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        srv = self.server
        ops = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv.patches.append((self.path, self.headers["Content-Type"], ops))
        if srv.reject_patch:
            return self._reply(400, {"message": "bad patch"})
        entity = srv.entities[self.path]
        for op in ops:  # only `remove`, applied in order as RFC 6902 does
            _, field, index = op["path"].split("/")
            del entity[field][int(index)]
        self._reply(200, entity)


@pytest.fixture
def om() -> Iterator[FakeServer]:
    server = FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_removes_every_default_role_and_each_no_owner_rule(om: FakeServer) -> None:
    _signup_baseline.restrict_signup_defaults(OpenMetadataClient(om.url))

    assert om.entities[ORG]["defaultRoles"] == []
    assert om.entities[POLICY]["rules"] == [{"name": "OrganizationPolicy-Owner-Rule"}]
    assert {content_type for _path, content_type, _ops in om.patches} == {"application/json-patch+json"}


def test_a_rerun_patches_nothing(om: FakeServer) -> None:
    client = OpenMetadataClient(om.url)
    _signup_baseline.restrict_signup_defaults(client)
    om.patches.clear()

    _signup_baseline.restrict_signup_defaults(client)

    assert om.patches == []


def test_a_refused_patch_fails_the_bootstrap(om: FakeServer) -> None:
    om.reject_patch = True

    with pytest.raises(OpenMetadataError, match="400"):
        _signup_baseline.restrict_signup_defaults(OpenMetadataClient(om.url))


def test_a_missing_policy_fails_the_bootstrap(om: FakeServer) -> None:
    del om.entities[POLICY]

    with pytest.raises(OpenMetadataError, match="404"):
        _signup_baseline.restrict_signup_defaults(OpenMetadataClient(om.url))


@pytest.mark.parametrize(("flags", "expected"), [([], 0), (["--sso"], 1)])
def test_the_command_applies_the_baseline_only_under_sso(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], expected: int
) -> None:
    from typer.testing import CliRunner

    from olf.cli import app
    from olf.infra.commands import runtime

    client = object()
    restricted: list[object] = []
    monkeypatch.setattr(runtime, "provider_contract_environment", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setenv("OPENLAKEFORGE_SHARED_NAMESPACE", "olf-shared")
    monkeypatch.setattr(_bootstrap, "bootstrap_auth", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(_stale_services, "remove_legacy_services", lambda _client: None)
    monkeypatch.setattr(_signup_baseline, "restrict_signup_defaults", restricted.append)

    result = CliRunner().invoke(app, ["infra", "openmetadata", "bootstrap", "--om-url", "http://om:8585", *flags])

    assert result.exit_code == 0, result.output
    assert restricted == [client] * expected
