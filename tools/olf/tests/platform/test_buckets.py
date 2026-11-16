"""`olf bucket` against a fake platform API (ADR 0021): each command's request, its output and its refusals."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from test_reads import GITHUB_PROFILE, _token
from typer.testing import CliRunner

from olf.cli import app
from olf.platform import api
from olf.platform import session as sessions

BASE = "/api/v1/stages/dev/buckets"
BUCKET = "acme-dev-bronze"
Answer = tuple[int, Any]


class FakeAPI:
    """Records every request and answers from `routes`, keyed by (method, path); a bytes answer is sent verbatim."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str], Answer | list[Answer]] = {}
        self.token = _token()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": url.path,
                        "query": parse_qs(url.query),
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "length": length,
                        "body": self.rfile.read(length),
                    }
                )
                answer = outer.routes.get((self.command, url.path), (404, {"detail": "Not Found"}))
                if isinstance(answer, list):
                    answer = answer.pop(0)
                status, data = answer
                payload = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_PUT = do_POST = handle_any  # noqa: N815 - http.server dispatches on these names

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api"

    def json_body(self) -> Any:
        return json.loads(self.requests[-1]["body"])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeAPI]:
    monkeypatch.setenv("OLF_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KUBECONFIG", "/nonexistent")
    monkeypatch.chdir(tmp_path)
    server = FakeAPI()
    threading.Thread(target=server.server.serve_forever, daemon=True).start()
    monkeypatch.setattr(api, "base_url", lambda instance: server.url)
    instance = sessions.resolve_instance(GITHUB_PROFILE)
    sessions.save(instance, sessions.Session(instance.issuer, server.token, "", "refresh"))
    yield server
    server.server.shutdown()


def _olf(*args: str, input: str | None = None) -> Any:  # noqa: A002 - CliRunner's own parameter name
    return CliRunner().invoke(app, ["bucket", *args, "-f", GITHUB_PROFILE], input=input)


def test_list_renders_a_table_and_json(fake: FakeAPI) -> None:
    buckets = [{"name": BUCKET, "created_at": "2026-10-01T00:00:00+00:00"}, {"name": "acme-ops", "created_at": None}]
    fake.routes[("GET", BASE)] = (200, {"buckets": buckets})

    table = _olf("list")
    as_json = _olf("list", "--json")

    assert table.exit_code == 0, table.output
    assert f"{BUCKET} | 2026-10-01T00:00:00+00:00" in table.stdout
    assert "acme-ops        | -" in table.stdout
    assert json.loads(as_json.stdout) == buckets
    assert fake.requests[0]["authorization"] == f"Bearer {fake.token}"


def test_list_takes_the_stage(fake: FakeAPI) -> None:
    fake.routes[("GET", "/api/v1/stages/uat/buckets")] = (200, {"buckets": []})

    assert _olf("list", "--stage", "uat").exit_code == 0
    assert fake.requests[0]["path"] == "/api/v1/stages/uat/buckets"


def test_ls_follows_next_cursor_and_lists_folders_then_files(fake: FakeAPI) -> None:
    path = f"{BASE}/{BUCKET}/objects"
    page1 = {
        "prefix": "raw/",
        "folders": [{"prefix": "raw/2026/"}],
        "files": [{"key": "raw/a.csv", "size": 12, "last_modified": "2026-10-01T00:00:00+00:00"}],
        "next_cursor": "c1",
    }
    page2 = {
        "prefix": "raw/",
        "folders": [],
        "files": [{"key": "raw/b.csv", "size": 3 * 1024 * 1024, "last_modified": "2026-10-02T00:00:00+00:00"}],
        "next_cursor": None,
    }
    fake.routes[("GET", path)] = [(200, page1), (200, page2)]

    result = _olf("ls", f"{BUCKET}/raw")

    assert result.exit_code == 0, result.output
    assert [r["query"] for r in fake.requests] == [{"prefix": ["raw/"]}, {"prefix": ["raw/"], "cursor": ["c1"]}]
    assert result.stdout.index("raw/2026/") < result.stdout.index("raw/a.csv") < result.stdout.index("raw/b.csv")
    assert "12 B" in result.stdout
    assert "3.0 MiB" in result.stdout
    assert "(3 rows)" in result.stdout


def test_ls_json_merges_every_page(fake: FakeAPI) -> None:
    path = f"{BASE}/{BUCKET}/objects"
    file = {"key": "a", "size": 1, "last_modified": "t"}
    fake.routes[("GET", path)] = [
        (200, {"prefix": "", "folders": [], "files": [file], "next_cursor": "c"}),
        (200, {"prefix": "", "folders": [{"prefix": "d/"}], "files": [], "next_cursor": None}),
    ]

    data = json.loads(_olf("ls", BUCKET, "--json").stdout)

    assert data == {"prefix": "", "folders": [{"prefix": "d/"}], "files": [file]}
    assert fake.requests[1]["query"] == {"cursor": ["c"]}


def test_get_streams_to_the_default_and_a_named_destination(fake: FakeAPI, tmp_path: Path) -> None:
    fake.routes[("GET", f"{BASE}/{BUCKET}/objects/content")] = (200, b"a,b\n1,2\n")

    first = _olf("get", f"{BUCKET}/raw/data file.csv")
    _olf("get", f"{BUCKET}/raw/data file.csv", str(tmp_path / "copy.csv"))

    assert first.exit_code == 0, first.output
    assert (tmp_path / "data file.csv").read_bytes() == b"a,b\n1,2\n"
    assert (tmp_path / "copy.csv").read_bytes() == b"a,b\n1,2\n"
    assert fake.requests[0]["query"] == {"key": ["raw/data file.csv"]}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_get_into_a_directory_and_refuses_a_folder(fake: FakeAPI, tmp_path: Path) -> None:
    fake.routes[("GET", f"{BASE}/{BUCKET}/objects/content")] = (200, b"x")
    (tmp_path / "out").mkdir()

    assert _olf("get", f"{BUCKET}/a/b.txt", str(tmp_path / "out")).exit_code == 0
    assert (tmp_path / "out" / "b.txt").read_bytes() == b"x"
    folder = _olf("get", f"{BUCKET}/a/")
    assert folder.exit_code == 1
    assert "a folder cannot be downloaded" in folder.stderr


def test_get_failure_leaves_no_file(fake: FakeAPI, tmp_path: Path) -> None:
    fake.routes[("GET", f"{BASE}/{BUCKET}/objects/content")] = (404, {"detail": "no such key"})

    result = _olf("get", f"{BUCKET}/missing.csv")

    assert result.exit_code == 1
    assert "no such key" in result.stderr
    assert list(tmp_path.glob("*missing*")) == []


def test_put_streams_the_file_with_its_length(fake: FakeAPI, tmp_path: Path) -> None:
    src = tmp_path / "orders.csv"
    src.write_bytes(b"id\n1\n")
    fake.routes[("PUT", f"{BASE}/{BUCKET}/objects/content")] = (201, {"key": "in/orders.csv", "size": 5})

    result = _olf("put", str(src), f"{BUCKET}/in/")

    assert result.exit_code == 0, result.output
    request = fake.requests[0]
    assert request["query"] == {"key": ["in/orders.csv"], "overwrite": ["false"]}
    assert request["content_type"] == "application/octet-stream"
    assert request["length"] == 5
    assert request["body"] == b"id\n1\n"
    assert "acme-dev-bronze/in/orders.csv (5 B)" in result.stdout


def test_put_to_a_key_with_overwrite(fake: FakeAPI, tmp_path: Path) -> None:
    src = tmp_path / "orders.csv"
    src.write_bytes(b"x")
    fake.routes[("PUT", f"{BASE}/{BUCKET}/objects/content")] = (201, {"key": "renamed.csv", "size": 1})

    assert _olf("put", str(src), f"{BUCKET}/renamed.csv", "--overwrite").exit_code == 0
    assert fake.requests[0]["query"] == {"key": ["renamed.csv"], "overwrite": ["true"]}


def test_put_to_the_bucket_root_keeps_the_file_name(fake: FakeAPI, tmp_path: Path) -> None:
    src = tmp_path / "orders.csv"
    src.write_bytes(b"x")
    fake.routes[("PUT", f"{BASE}/{BUCKET}/objects/content")] = (201, {"key": "orders.csv", "size": 1})

    assert _olf("put", str(src), BUCKET).exit_code == 0
    assert fake.requests[0]["query"]["key"] == ["orders.csv"]


@pytest.mark.parametrize(("status", "detail"), [(409, "object exists"), (413, "upload exceeds the limit")])
def test_put_reports_a_conflict_and_a_too_large_upload(fake: FakeAPI, tmp_path: Path, status: int, detail: str) -> None:
    src = tmp_path / "f.csv"
    src.write_bytes(b"x")
    fake.routes[("PUT", f"{BASE}/{BUCKET}/objects/content")] = (status, {"detail": detail})

    result = _olf("put", str(src), f"{BUCKET}/f.csv")

    assert result.exit_code == 1
    assert f"HTTP {status}: {detail}" in result.stderr


def test_put_refuses_a_missing_file(fake: FakeAPI, tmp_path: Path) -> None:
    result = _olf("put", str(tmp_path / "nope"), f"{BUCKET}/x")

    assert result.exit_code == 1
    assert "is not a file" in result.stderr
    assert fake.requests == []


def test_mkdir_posts_the_prefix_with_a_trailing_slash(fake: FakeAPI) -> None:
    fake.routes[("POST", f"{BASE}/{BUCKET}/folders")] = (201, {"prefix": "a/b/"})

    result = _olf("mkdir", f"{BUCKET}/a/b")

    assert result.exit_code == 0, result.output
    assert fake.json_body() == {"prefix": "a/b/"}
    assert "created acme-dev-bronze/a/b/" in result.stdout


def test_mv_posts_from_and_to_for_a_file_and_a_folder(fake: FakeAPI) -> None:
    fake.routes[("POST", f"{BASE}/{BUCKET}/objects/rename")] = (200, {"moved": 3})

    file = _olf("mv", f"{BUCKET}/a.csv", "b/a.csv")
    folder = _olf("mv", f"{BUCKET}/old/", "new/")

    assert file.exit_code == folder.exit_code == 0
    assert [json.loads(r["body"]) for r in fake.requests] == [
        {"from": "a.csv", "to": "b/a.csv"},
        {"from": "old/", "to": "new/"},
    ]
    assert "moved 3 object(s)" in folder.stdout


def test_mv_refuses_a_file_to_folder_mismatch(fake: FakeAPI) -> None:
    result = _olf("mv", f"{BUCKET}/old/", "new")

    assert result.exit_code == 1
    assert "both keys end in '/'" in result.stderr
    assert fake.requests == []


def test_rm_asks_first_and_aborts_on_no(fake: FakeAPI) -> None:
    result = _olf("rm", f"{BUCKET}/dir/", input="n\n")

    assert result.exit_code == 1
    assert "everything under acme-dev-bronze/dir/" in result.stdout
    assert fake.requests == []


def test_rm_deletes_after_a_yes_or_with_the_flag(fake: FakeAPI) -> None:
    fake.routes[("POST", f"{BASE}/{BUCKET}/objects/delete")] = (200, {"deleted": ["a.csv"], "errors": []})

    asked = _olf("rm", f"{BUCKET}/a.csv", input="y\n")
    flagged = _olf("rm", f"{BUCKET}/a.csv", "--yes")

    assert asked.exit_code == flagged.exit_code == 0
    assert [json.loads(r["body"]) for r in fake.requests] == [{"keys": ["a.csv"]}] * 2
    assert "deleted 1 key(s)" in flagged.stdout


def test_rm_prints_per_key_errors_and_exits_non_zero(fake: FakeAPI) -> None:
    fake.routes[("POST", f"{BASE}/{BUCKET}/objects/delete")] = (
        200,
        {"deleted": [], "errors": [{"key": "audit/x", "detail": "read-only prefix"}]},
    )

    result = _olf("rm", f"{BUCKET}/audit/x", "--yes")

    assert result.exit_code == 1
    assert "audit/x" in result.stderr
    assert "read-only prefix" in result.stderr


def test_rm_refuses_a_whole_bucket(fake: FakeAPI) -> None:
    result = _olf("rm", BUCKET, "--yes")

    assert result.exit_code == 1
    assert fake.requests == []


def test_a_target_without_a_bucket_is_refused(fake: FakeAPI) -> None:
    result = _olf("ls", "/")

    assert result.exit_code == 1
    assert "name a bucket" in result.stderr


def test_a_refused_session_says_run_olf_login(fake: FakeAPI) -> None:
    fake.routes[("GET", BASE)] = (401, {"detail": "token refused"})

    result = _olf("list")

    assert result.exit_code == 1
    assert "run `olf login`" in result.stderr


def test_a_role_without_access_names_what_it_needs(fake: FakeAPI) -> None:
    fake.routes[("GET", BASE)] = (403, {"detail": "role 'analyst' may not perform 'list_buckets'"})

    result = _olf("list")

    assert result.exit_code == 1
    assert "may not perform 'list_buckets'" in result.stderr


def test_a_cut_off_download_is_reported_and_leaves_no_file(
    monkeypatch: pytest.MonkeyPatch, fake: FakeAPI, tmp_path: Path
) -> None:
    class Cut:
        ok = True
        status_code = 200

        def __enter__(self) -> Cut:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def iter_content(self, chunk_size: int) -> Iterator[bytes]:
            yield b"partial"
            raise requests.exceptions.ChunkedEncodingError("dropped")

    monkeypatch.setattr(sessions, "send", lambda *a, **k: Cut())

    result = _olf("get", f"{BUCKET}/big.bin")

    assert result.exit_code == 1
    assert "the download was cut off" in result.stderr
    assert list(tmp_path.glob("*big.bin*")) == []
