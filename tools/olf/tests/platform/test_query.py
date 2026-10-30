"""`olf query`: a person's own SQL through Trino's query_api route, against a fake Trino (ADR 0019)."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.platform import query as person_query
from olf.platform import session as sessions

GITHUB_PROFILE = str(Path(__file__).parents[1] / "fixtures/profiles/valid_github_identity.yaml")
EMAIL = "ada@example.com"


def _jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature-not-checked"


TOKEN = _jwt(
    {
        "exp": int(time.time()) + 300,
        "email": EMAIL,
        "groups": ["acme:analysts"],
        "federated_claims": {"connector_id": "github"},
    }
)


class FakeTrino:
    """POST /v1/statement then one nextUri, as Trino pages a result; or a canned refusal."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.error: dict[str, Any] | None = None
        self.url = ""
        trino = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, body: dict[str, Any]) -> None:
                self.send_response(trino.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                trino.requests.append({"headers": dict(self.headers), "sql": self.rfile.read(length).decode()})
                if trino.error is not None:
                    self._reply({"error": trino.error})
                    return
                self._reply(
                    {"columns": [{"name": "id"}, {"name": "name"}], "data": [[1, "a"]], "nextUri": f"{trino.url}/next"}
                )

            def do_GET(self) -> None:
                trino.requests.append({"headers": dict(self.headers)})
                self._reply({"data": [[2, None]]})

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/statement"


@pytest.fixture
def trino(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeTrino]:
    monkeypatch.setenv("OLF_HOME", str(tmp_path / "home"))
    fake = FakeTrino()
    threading.Thread(target=fake.server.serve_forever, daemon=True).start()
    monkeypatch.setattr(person_query, "statement_url", lambda instance: fake.url)
    yield fake
    fake.server.shutdown()


def _sign_in(token: str = TOKEN) -> None:
    instance = sessions.resolve_instance(GITHUB_PROFILE)
    sessions.save(instance, sessions.Session(instance.issuer, token, "", "refresh"))


def _query(*args: str) -> Any:
    return CliRunner().invoke(app, ["query", "-f", GITHUB_PROFILE, *args])


def test_a_query_carries_the_person_token_twice_and_their_email_and_stage(trino: FakeTrino) -> None:
    _sign_in()

    result = _query("--stage", "prod", "SELECT * FROM sales_gold.orders")

    assert result.exit_code == 0, result.output
    assert result.stdout == "id | name\n---+-----\n1  | a   \n2  | NULL\n(2 rows)\n"
    first = trino.requests[0]
    assert first["sql"] == "SELECT * FROM sales_gold.orders"
    assert first["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert first["headers"]["X-Trino-Extra-Credential"] == f"token={TOKEN}"
    assert first["headers"]["X-Trino-User"] == EMAIL
    assert first["headers"]["X-Trino-Catalog"] == "prod"
    assert trino.requests[1]["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("output_format", "expected"),
    [
        ("csv", "id,name\n1,a\n2,\n"),
        ("json", '{"id": 1, "name": "a"}\n{"id": 2, "name": null}\n'),
    ],
)
def test_csv_and_json_output(trino: FakeTrino, output_format: str, expected: str) -> None:
    _sign_in()

    result = _query("--format", output_format, "SELECT 1")

    assert result.exit_code == 0, result.output
    assert result.stdout == expected


def _failure(*messages: str) -> dict[str, Any]:
    info: dict[str, Any] | None = None
    for message in reversed(messages):
        info = {"message": message, "cause": info}
    return {"message": messages[0], "failureInfo": info}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            _failure(
                "Failed to list namespaces",
                "Forbidden: Principal 'github' with activated PrincipalRoles '[olf-signed-in, olf-analyst]' "
                "is not authorized for op LIST_TABLES",
            ),
            "Polaris refused this on stage dev: your role does not reach it",
        ),
        (
            _failure(
                "Error committing write to Parquet file", "Put failed for bucket [gold]", "Access Denied. (Service: S3)"
            ),
            "your role may only read on stage dev",
        ),
        (_failure("Access Denied: Cannot access catalog lakehouse_dev"), "only the people catalogs dev, uat, prod"),
    ],
)
def test_a_refusal_is_explained_from_the_cause_chain(trino: FakeTrino, error: dict[str, Any], expected: str) -> None:
    _sign_in()
    trino.error = error

    result = _query("SELECT 1")

    assert result.exit_code == 1
    assert expected in result.stderr


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "run `olf login`"), (403, "your GitHub teams map to no OpenLakeForge role"), (404, "no query route")],
)
def test_the_perimeter_or_trino_refusing_the_request_says_what_to_do(
    trino: FakeTrino, status: int, expected: str
) -> None:
    _sign_in()
    trino.status = status

    result = _query("SELECT 1")

    assert result.exit_code == 1
    assert expected in result.stderr


def test_a_session_from_before_olf_query_asks_to_sign_in_again(trino: FakeTrino) -> None:
    _sign_in(_jwt({"exp": int(time.time()) + 300, "email": EMAIL, "groups": ["acme:analysts"]}))

    result = _query("SELECT 1")

    assert result.exit_code == 1
    assert "run `olf login` again" in result.stderr
    assert trino.requests == []


def test_a_deployment_without_github_sign_in_points_to_the_workload_path(tmp_path: Path) -> None:
    profile = tmp_path / "openlakeforge.yaml"
    profile.write_text(
        "apiVersion: openlakeforge.io/v1alpha1\nkind: DeploymentProfile\nmetadata: {name: fixture}\n"
        "spec:\n  provider: {type: local}\n  preset: slim\n  stages: {dev: {enabled: true}}\n"
        "  access: {base_domain: olf.example.com}\n"
        "  identity: {issuer: external, issuer_url: 'https://login.example.com', role_claim: groups,"
        " role_mapping: {platform-admin: [admins]}}\n"
    )

    result = CliRunner().invoke(app, ["query", "-f", str(profile), "SELECT 1"])

    assert result.exit_code == 1
    assert "olf infra query" in result.stderr
