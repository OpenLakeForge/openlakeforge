"""The per-stage quality-writer bots against a stateful fake OpenMetadata, with Secrets held in a dict (ADR 0007)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import hcl2
import pytest
from openlakeforge_services.openmetadata import OpenMetadataClient

from olf.infra import access, k8s
from olf.infra.openmetadata import _platform, _quality_writer
from olf.infra.openmetadata._quality_writer import (
    JWT_KEY,
    POLICY,
    ROLE,
    RULES,
    ensure_quality_writers,
    writer_bot,
    writer_credentials,
)
from olf.infra.openmetadata._stage_services import StageServices

REPO = Path(__file__).resolve().parents[4]
NS = "olf-system"
DEFAULT_BOT_ROLE = {"id": "id-DefaultBotRole", "type": "role", "name": "DefaultBotRole"}


class FakeServer(ThreadingHTTPServer):
    """Policies, roles, bots and bot users as OpenMetadata 1.13.6 keeps them, including the DefaultBotRole that
    every bot user put through /api/v1/users gains (UserResource.addRolesToBot)."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.policies: dict[str, dict] = {}
        self.roles: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.bots: set[str] = set()
        self.tokens: dict[str, str] = {}  # user id -> JWT
        self.minted = 0


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
        srv, path, method = self.server, urlsplit(self.path).path, self.command
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if bearer != "admin-jwt":
            user = next((u for u in srv.users.values() if srv.tokens.get(u["id"]) == bearer), None)
            if path == "/api/v1/users/loggedInUser" and user is not None:
                return self._reply(200, user)
            return self._reply(401, {})
        if (method, path) == ("PUT", "/api/v1/policies"):
            srv.policies[body["name"]] = body
            return self._reply(200, {"id": f"id-{body['name']}"})
        if (method, path) == ("PUT", "/api/v1/roles"):
            srv.roles[body["name"]] = body
            return self._reply(200, {"id": f"id-{body['name']}"})
        if (method, path) == ("PUT", "/api/v1/users"):
            existing = srv.users.get(body["name"])
            roles = [DEFAULT_BOT_ROLE, *({"id": r, "type": "role"} for r in body.get("roles") or [])]
            user = {"id": f"id-{body['name']}", "name": body["name"], "isAdmin": body["isAdmin"], "isBot": True}
            srv.users[body["name"]] = {**user, "roles": roles + (existing or {}).get("roles", [])}
            return self._reply(201, user)
        if (method, path) == ("PUT", "/api/v1/bots"):
            srv.bots.add(body["name"])
            return self._reply(201, {})
        if path.startswith("/api/v1/users/name/"):
            name = path.rsplit("/", 1)[1]
            if method == "DELETE":
                return self._reply(200 if srv.users.pop(name, None) else 404, {})
            found = srv.users.get(name)
            return self._reply(200 if found else 404, found or {})
        if path.startswith("/api/v1/bots/name/") and method == "DELETE":
            name = path.rsplit("/", 1)[1]
            if name not in srv.bots:
                return self._reply(404, {})
            srv.bots.discard(name)
            srv.users.pop(name, None)  # recursive: the bot contains its user
            return self._reply(200, {})
        if path.startswith("/api/v1/users/generateToken/"):
            srv.minted += 1
            srv.tokens[path.rsplit("/", 1)[1]] = f"jwt-{srv.minted}"
            return self._reply(200, {})
        if path.startswith("/api/v1/users/token/"):
            return self._reply(200, {"JWTToken": srv.tokens[path.rsplit("/", 1)[1]]})
        if method == "PATCH" and path.startswith("/api/v1/users/"):
            assert self.headers["Content-Type"] == "application/json-patch+json"
            user = next(u for u in srv.users.values() if u["id"] == path.rsplit("/", 1)[1])
            for op in body:
                if op["path"] == "/roles" and op["op"] in ("add", "replace"):
                    user["roles"] = op["value"]
            return self._reply(200, user)
        self._reply(404, {})

    do_GET = do_PUT = do_POST = do_PATCH = do_DELETE = _handle  # noqa: N815 - BaseHTTPRequestHandler API


@pytest.fixture
def om() -> Iterator[FakeServer]:
    server = FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], dict[str, str]]:
    """Kubernetes Secrets keyed by (namespace, name)."""
    secrets: dict[tuple[str, str], dict[str, str]] = {}

    def secret_value(name: str, key: str, namespace: str, **kwargs: object) -> str:
        if (namespace, name) not in secrets:
            if kwargs.get("missing_ok"):
                return ""
            raise k8s.KubectlError("NotFound")
        return secrets[(namespace, name)].get(key, "")

    def write_secret(name: str, namespace: str, data: dict[str, str], **_kwargs: object) -> None:
        secrets[(namespace, name)] = dict(data)

    monkeypatch.setattr(k8s, "secret_value", secret_value)
    monkeypatch.setattr(k8s, "write_secret", write_secret)
    monkeypatch.setattr(k8s, "secret_data", lambda name, namespace, **_kw: secrets.get((namespace, name)))
    monkeypatch.setattr(k8s, "delete_secret", lambda name, namespace, **_kw: secrets.pop((namespace, name)))
    return secrets


def _reconcile(om: FakeServer, *stages: str) -> None:
    route = access.Route(om.url)
    ensure_quality_writers(route.client(OpenMetadataClient, token="admin-jwt"), route, NS, list(stages))


def _grants() -> set[tuple[str, str]]:
    return {(resource, op) for rule in RULES for resource in rule["resources"] for op in rule["operations"]}


def test_the_role_grants_exactly_what_the_publishers_calls_are_authorized_against() -> None:
    assert _grants() == {
        ("table", "ViewBasic"),
        ("table", "ViewTests"),
        ("table", "EditTests"),
        ("testDefinition", "Create"),
        ("testDefinition", "EditAll"),
    }
    assert all(rule["effect"] == "allow" and "condition" not in rule for rule in RULES)


def test_the_role_edits_only_tests_reads_no_row_sample_and_holds_nothing_on_lineage_or_every_resource() -> None:
    grants = _grants()
    assert not {op for resource, op in grants if resource == "table"} - {"ViewBasic", "ViewTests", "EditTests"}
    assert not {resource for resource, _ in grants} & {"all", "All", "*", "lineage", "bot", "user", "role", "policy"}
    # ViewAll would match ViewSampleData on a table and ViewTestCaseFailedRowsSample on a case: row samples.
    assert not {op for _, op in grants} & {"All", "ViewAll", "EditDescription", "EditLineage", "EditTags", "Delete"}
    assert not {op for resource, op in grants if resource != "testDefinition"} & {"EditAll", "Create"}


def test_each_governed_stage_gets_its_own_bot_holding_only_the_writer_role(
    om: FakeServer, store: dict[tuple[str, str], dict[str, str]]
) -> None:
    _reconcile(om, "dev", "prod")

    assert om.policies[POLICY]["rules"] == list(RULES)
    assert om.roles[ROLE]["policies"] == [POLICY]
    for stage in ("dev", "prod"):
        user = om.users[writer_bot(stage)]
        assert user["isBot"] and not user["isAdmin"]
        # DefaultBotRole carries DataConsumerPolicy, which would let the writer edit any description and tag.
        assert [role["id"] for role in user["roles"]] == [f"id-{ROLE}"]
        assert store[(NS, writer_credentials(stage))] == {JWT_KEY: om.tokens[user["id"]]}
    assert om.bots == {writer_bot("dev"), writer_bot("prod")}
    assert store[(NS, writer_credentials("dev"))] != store[(NS, writer_credentials("prod"))]


def test_a_rerun_keeps_working_tokens_and_puts_back_a_drifted_role(
    om: FakeServer, store: dict[tuple[str, str], dict[str, str]]
) -> None:
    _reconcile(om, "dev")
    token = store[(NS, writer_credentials("dev"))][JWT_KEY]
    om.users[writer_bot("dev")]["roles"].append(DEFAULT_BOT_ROLE)
    om.policies[POLICY]["rules"] = [{"name": "widened", "resources": ["all"], "operations": ["All"]}]

    _reconcile(om, "dev")

    assert store[(NS, writer_credentials("dev"))][JWT_KEY] == token
    assert om.minted == 1
    assert [role["id"] for role in om.users[writer_bot("dev")]["roles"]] == [f"id-{ROLE}"]
    assert om.policies[POLICY]["rules"] == list(RULES)


def test_a_stale_or_missing_token_is_replaced(om: FakeServer, store: dict[tuple[str, str], dict[str, str]]) -> None:
    _reconcile(om, "dev")
    store[(NS, writer_credentials("dev"))] = {JWT_KEY: "revoked"}

    _reconcile(om, "dev")

    user_id = om.users[writer_bot("dev")]["id"]
    assert store[(NS, writer_credentials("dev"))] == {JWT_KEY: om.tokens[user_id]} != {JWT_KEY: "revoked"}


def test_a_stage_that_loses_governance_loses_its_bot_and_its_secret(
    om: FakeServer, store: dict[tuple[str, str], dict[str, str]]
) -> None:
    _reconcile(om, "dev", "prod")

    _reconcile(om, "dev")

    assert writer_bot("prod") not in om.users and writer_bot("prod") not in om.bots
    assert (NS, writer_credentials("prod")) not in store
    assert (NS, writer_credentials("dev")) in store


def test_the_bootstrap_reconciles_writers_for_its_stages_only_when_it_has_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(_platform._bootstrap, "bootstrap_auth", lambda *_a, **_k: "client")
    monkeypatch.setattr(_platform._stage_services, "bootstrap_stage_services", lambda *_a, **_k: None)
    monkeypatch.setattr(_platform._stale_services, "remove_legacy_services", lambda _c: None)
    monkeypatch.setattr(_quality_writer, "ensure_quality_writers", lambda *args: calls.append(args))
    route = access.Route("http://om:8585")
    stages = (StageServices("dev", "http://d"), StageServices("prod", "http://p"))

    _platform.run_bootstrap(route, NS, email="a@b", sso=False, seed=None, stages=())
    assert calls == []
    _platform.run_bootstrap(route, NS, email="a@b", sso=False, seed=None, stages=stages, environ={})
    assert calls == [("client", route, NS, ["dev", "prod"], {})]


def test_the_reconciler_module_reads_the_secret_and_key_the_bootstrap_writes() -> None:
    module = REPO / "infra/terraform/modules/governance/quality-reconciler/variables.tf"
    variables = {name: body for block in hcl2.loads(module.read_text())["variable"] for name, body in block.items()}
    assert variables["writer_secret_prefix"]["default"] + "dev" == writer_credentials("dev")
    assert variables["writer_jwt_key"]["default"] == JWT_KEY
