"""The platform read commands against a fake platform API (ADR 0021): the token sent, the output, and each refusal."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.platform import api
from olf.platform import session as sessions

GITHUB_PROFILE = str(Path(__file__).parents[1] / "fixtures/profiles/valid_github_identity.yaml")


def _jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature-not-checked"


TOKEN = _jwt({"exp": int(time.time()) + 300, "email": "ada@example.com", "groups": ["acme:data-engineers"]})

RUN = {
    "run_id": "6c1f3a5e-0000-4000-8000-000000000001",
    "job_name": "orders_job",
    "status": "SUCCESS",
    "created_at": 1761800000.0,
    "started_at": 1761800001.0,
    "ended_at": 1761800091.0,
    "tags": {"dagster/from_ui": "true"},
}
ANSWERS: dict[str, Any] = {
    "/api/v1/stages/dev/jobs": {
        "jobs": [
            {"location": "project-code", "repository": "__repository__", "name": "orders_job", "description": "Orders"}
        ]
    },
    "/api/v1/stages/dev/runs": {"runs": [RUN]},
    f"/api/v1/stages/dev/runs/{RUN['run_id']}": RUN,
    "/api/v1/stages/dev/logs": {
        "lines": [
            {
                "archived_at": "2026-10-30T09:00:00+00:00",
                "namespace": "olf-dev",
                "pod": "run-1",
                "container": "main",
                "line": "2026-10-30T08:59:58Z started",
                "error": None,
            },
            {
                "archived_at": "2026-10-30T09:00:00+00:00",
                "namespace": "olf-dev",
                "pod": "run-1",
                "container": "sidecar",
                "line": None,
                "error": "container not started",
            },
        ],
        "truncated": False,
    },
    "/api/v1/domains": {"domains": [{"name": "sales", "display_name": "Sales"}]},
    "/api/v1/data-products": {
        "data_products": [{"name": "orders_mart", "display_name": "Orders", "domains": ["sales", "finance"]}]
    },
}


LAUNCHED = {"run_id": "run-new", "job": "orders_job", "stage": "dev", "request_id": "0" * 32}
POST_ANSWERS: dict[str, Any] = {"/api/v1/stages/dev/runs": LAUNCHED}


class FakeAPI:
    """Answers each known path with its JSON; `status` and `body` replace every answer with a refusal."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: bytes | None = None
        api_ = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's handler name
                self.answer(ANSWERS, 200)

            def do_POST(self) -> None:  # noqa: N802
                self.answer(POST_ANSWERS, 202)

            def answer(self, answers: dict[str, Any], ok: int) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                api_.requests.append(
                    {
                        "method": self.command,
                        "path": url.path,
                        "query": parse_qs(url.query),
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(self.rfile.read(length)) if length else None,
                    }
                )
                if api_.status != 200:
                    payload, status = api_.body or b"", api_.status
                elif url.path in answers:
                    payload, status = json.dumps(answers[url.path]).encode(), ok
                else:
                    payload, status = json.dumps({"detail": "Not Found"}).encode(), 404
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api"

    def refuse(self, status: int, detail: str | None) -> None:
        self.status = status
        self.body = json.dumps({"detail": detail}).encode() if detail is not None else b"<html>Forbidden</html>"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeAPI]:
    monkeypatch.setenv("OLF_HOME", str(tmp_path / "home"))
    # Platform commands need no cluster access: a kubeconfig that does not exist changes nothing.
    monkeypatch.setenv("KUBECONFIG", "/nonexistent")
    server = FakeAPI()
    threading.Thread(target=server.server.serve_forever, daemon=True).start()
    monkeypatch.setattr(api, "base_url", lambda instance: server.url)
    instance = sessions.resolve_instance(GITHUB_PROFILE)
    sessions.save(instance, sessions.Session(instance.issuer, TOKEN, "", "refresh"))
    yield server
    server.server.shutdown()


def _olf(*args: str) -> Any:
    return CliRunner().invoke(app, [*args, "-f", GITHUB_PROFILE])


COMMANDS = [
    ("jobs", "list"),
    ("runs", "list"),
    ("runs", "show", RUN["run_id"]),
    ("runs", "launch", "orders_job"),
    ("logs",),
    ("domain", "list"),
    ("product", "list"),
]


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_sends_the_session_token_and_nothing_else(fake: FakeAPI, command: tuple[str, ...]) -> None:
    result = _olf(*command)

    assert result.exit_code == 0, result.output
    assert [request["authorization"] for request in fake.requests] == [f"Bearer {TOKEN}"]


def test_jobs_list_renders_a_table(fake: FakeAPI) -> None:
    result = _olf("jobs", "list")

    assert result.stdout == (
        "   job     |   location   | description\n"
        "-----------+--------------+------------\n"
        "orders_job | project-code | Orders     \n"
        "(1 row)\n"
    )
    assert fake.requests[0]["path"] == "/api/v1/stages/dev/jobs"


def test_runs_list_passes_the_stage_job_and_limit(fake: FakeAPI) -> None:
    result = _olf("runs", "list", "--stage", "dev", "--job", "orders_job", "--limit", "5")

    assert result.exit_code == 0, result.output
    assert fake.requests[0]["query"] == {"job": ["orders_job"], "limit": ["5"]}
    assert "orders_job | SUCCESS | 2025-10-30T04:53:20+00:00 | 90s" in result.stdout


def test_runs_show_lists_the_runs_fields_and_tags(fake: FakeAPI) -> None:
    result = _olf("runs", "show", RUN["run_id"])

    assert result.exit_code == 0, result.output
    rows = {line.split("|")[0].strip(): line.split("|")[1].strip() for line in result.stdout.splitlines()[2:-1]}
    assert rows["status"] == "SUCCESS"
    assert rows["duration"] == "90s"
    assert rows["tag dagster/from_ui"] == "true"


def test_product_list_joins_a_products_domains(fake: FakeAPI) -> None:
    assert "orders_mart | Orders       | sales, finance" in _olf("product", "list").stdout


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (("jobs", "list"), ANSWERS["/api/v1/stages/dev/jobs"]["jobs"]),
        (("runs", "show", RUN["run_id"]), RUN),
        (("domain", "list"), ANSWERS["/api/v1/domains"]["domains"]),
        (("product", "list"), ANSWERS["/api/v1/data-products"]["data_products"]),
    ],
)
def test_json_prints_the_apis_data(fake: FakeAPI, command: tuple[str, ...], expected: Any) -> None:
    result = _olf(*command, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("command", COMMANDS)
def test_a_refused_session_says_run_olf_login(fake: FakeAPI, command: tuple[str, ...]) -> None:
    fake.refuse(401, "token refused: Signature has expired")

    result = _olf(*command)

    assert result.exit_code == 1
    assert "run `olf login`" in result.stderr
    assert TOKEN not in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_a_refusal_below_the_floor_names_the_role_the_api_needs(fake: FakeAPI, command: tuple[str, ...]) -> None:
    fake.refuse(403, "role 'analyst' may not perform 'list_runs'; it needs 'data-engineer' or above")

    result = _olf(*command)

    assert result.exit_code == 1
    assert "it needs 'data-engineer' or above" in result.stderr


def test_the_perimeters_own_refusal_says_no_role_is_assigned(fake: FakeAPI) -> None:
    fake.refuse(403, None)

    result = _olf("jobs", "list")

    assert result.exit_code == 1
    assert "the perimeter refused the request (HTTP 403); most often your GitHub teams map to no" in result.stderr


def test_a_stage_the_deployment_does_not_enable_is_reported_by_the_api(fake: FakeAPI) -> None:
    fake.refuse(404, "stage 'qa' is not enabled")

    result = _olf("runs", "list", "--stage", "qa")

    assert result.exit_code == 1
    assert "stage 'qa' is not enabled" in result.stderr


def test_a_deployment_without_the_platform_api_says_so(fake: FakeAPI) -> None:
    fake.refuse(404, None)

    result = _olf("domain", "list")

    assert result.exit_code == 1
    assert "serves no platform API" in result.stderr


def test_a_service_failure_passes_on_the_apis_explanation(fake: FakeAPI) -> None:
    fake.refuse(502, "Dagster for stage 'dev' is unreachable")

    result = _olf("jobs", "list")

    assert result.exit_code == 1
    assert "HTTP 502: Dagster for stage 'dev' is unreachable" in result.stderr


def test_without_a_session_nothing_is_sent(fake: FakeAPI) -> None:
    sessions.delete(sessions.resolve_instance(GITHUB_PROFILE))

    result = _olf("jobs", "list")

    assert result.exit_code == 1
    assert "run `olf login`" in result.stderr
    assert fake.requests == []


def test_the_api_is_on_the_portals_host_with_the_issuers_scheme() -> None:
    instance = sessions.Instance("olf.localhost", "https://auth.olf.localhost", "olf-cli", dex=True)

    assert api.base_url(instance) == "https://olf.localhost/api"
    assert api.base_url(sessions.Instance("olf.localhost", "http://auth.olf.localhost", "olf-cli")) == (
        "http://olf.localhost/api"
    )


def test_runs_launch_posts_the_job_once_and_says_how_to_follow_the_run(fake: FakeAPI) -> None:
    result = _olf("runs", "launch", "orders_job", "--stage", "dev")

    assert result.exit_code == 0, result.output
    assert [(r["method"], r["path"], r["body"]) for r in fake.requests] == [
        ("POST", "/api/v1/stages/dev/runs", {"job": "orders_job"})
    ]
    assert result.stdout == (
        "Launched orders_job on dev: run run-new\nFollow it with `olf runs show run-new --stage dev`.\n"
    )


def test_runs_launch_json_is_the_apis_answer(fake: FakeAPI) -> None:
    assert json.loads(_olf("runs", "launch", "orders_job", "--json").stdout) == LAUNCHED


def test_a_launch_the_api_could_not_audit_is_reported_and_not_retried(fake: FakeAPI) -> None:
    fake.refuse(503, "the audit record could not be stored; nothing was done")

    result = _olf("runs", "launch", "orders_job")

    assert result.exit_code == 1
    assert "nothing was done" in result.stderr
    assert len(fake.requests) == 1


def test_logs_prints_each_line_with_its_source_and_an_unread_container_as_such(fake: FakeAPI) -> None:
    result = _olf("logs", "--date", "2026-10-30")

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "run-1/main  2026-10-30T08:59:58Z started\nrun-1/sidecar  [not archived: container not started]\n"
    )
    assert result.stderr == ""


def test_logs_passes_the_window_and_filters(fake: FakeAPI) -> None:
    result = _olf(
        "logs", "--stage", "dev", "--date", "2026-10-30", "--hour", "0", "--pod", "run-1", "--container", "main",
        "--limit", "50",
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert fake.requests[0]["path"] == "/api/v1/stages/dev/logs"
    assert fake.requests[0]["query"] == {
        "date": ["2026-10-30"], "hour": ["0"], "pod": ["run-1"], "container": ["main"], "limit": ["50"],
    }  # fmt: skip


def test_logs_reads_today_in_utc_by_default(fake: FakeAPI) -> None:
    from datetime import UTC, datetime

    _olf("logs")

    assert fake.requests[0]["query"]["date"] == [datetime.now(UTC).date().isoformat()]
    assert "hour" not in fake.requests[0]["query"]


def test_a_truncated_answer_says_how_to_narrow_it(fake: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ANSWERS, "/api/v1/stages/dev/logs", {**ANSWERS["/api/v1/stages/dev/logs"], "truncated": True})

    result = _olf("logs", "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "narrow with --hour, --pod or --container, or raise --limit" in result.stderr


def test_a_record_with_neither_line_nor_error_prints_an_empty_line(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = {**ANSWERS["/api/v1/stages/dev/logs"]["lines"][0], "line": None}
    monkeypatch.setitem(ANSWERS, "/api/v1/stages/dev/logs", {"lines": [bare], "truncated": False})

    result = _olf("logs")

    assert result.exit_code == 0, result.output
    assert result.stdout == "run-1/main  \n"


def test_logs_json_is_the_apis_answer_with_truncated(fake: FakeAPI) -> None:
    result = _olf("logs", "--json")

    assert json.loads(result.stdout) == ANSWERS["/api/v1/stages/dev/logs"]


def test_a_date_that_is_not_one_is_refused_before_any_request(fake: FakeAPI) -> None:
    result = _olf("logs", "--date", "yesterday")

    assert result.exit_code == 1
    assert "not a date in YYYY-MM-DD form" in result.stderr
    assert fake.requests == []
