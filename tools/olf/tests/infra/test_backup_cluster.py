"""`olf infra backup`'s cluster bindings that decide what reaches disk (#910)."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from _backup_support import full_contracts
from typer.testing import CliRunner

from olf.cli import app
from olf.infra import k8s
from olf.infra.backup import BackupError
from olf.infra.backup._cluster import _copy_bucket, cluster_operations


@contextlib.contextmanager
def _server(label: str, hits: list[str]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's name
            hits.append(label)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"data": {"runsOrError": {"__typename": "Runs", "results": []}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


class FakeS3:
    def __init__(self, objects: dict[str, bytes], *, listed_size: dict[str, int] | None = None) -> None:
        self.objects = objects
        self.listed_size = listed_size or {}

    def get_paginator(self, _: str) -> Any:
        listing = [{"Key": key, "Size": self.listed_size.get(key, len(body))} for key, body in self.objects.items()]
        return type("Paginator", (), {"paginate": lambda _self, Bucket: [{"Contents": listing}]})()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[key])


def test_every_object_lands_under_its_key_and_a_folder_marker_is_not_one(tmp_path: Path) -> None:
    client = FakeS3({"iceberg/t/a.parquet": b"abc", "iceberg/": b"", "b.json": b"{}"})

    inventory = _copy_bucket(client, "lakehouse-dev-gold", tmp_path / "lakehouse-dev-gold")

    assert inventory == {"iceberg/t/a.parquet": 3, "b.json": 2}
    assert (tmp_path / "lakehouse-dev-gold" / "iceberg" / "t" / "a.parquet").read_bytes() == b"abc"


def test_a_non_empty_object_under_a_trailing_slash_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match=r"'dir/' in bucket b ends in '/' but holds 3 bytes"):
        _copy_bucket(FakeS3({"dir/": b"abc"}), "b", tmp_path / "b")


def test_a_key_with_a_line_break_is_refused_before_it_is_downloaded(tmp_path: Path) -> None:
    client = FakeS3({"a\nb": b"x"})

    with pytest.raises(BackupError, match="line break"):
        _copy_bucket(client, "b", tmp_path / "b")

    assert not (tmp_path / "b" / "a\nb").exists()


def test_the_dagster_check_bypasses_the_proxy_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    hits: list[str] = []
    with _server("proxy", hits) as proxy, _server("dagster", hits) as dagster:
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("http_proxy", proxy)

        @contextlib.contextmanager
        def forwarded(*_: Any, **__: Any) -> Iterator[int]:
            yield int(dagster.rsplit(":", 1)[1])

        monkeypatch.setattr(k8s, "port_forward", forwarded)
        with cluster_operations(full_contracts(), shared_namespace="olf-system", environ={}) as ops:
            assert ops.runs_in_flight("olf-dev") == []

    assert hits == ["dagster"]


def test_a_key_that_climbs_out_of_the_backup_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="outside the backup"):
        _copy_bucket(FakeS3({"../escape": b"x"}), "b", tmp_path / "b")

    assert not (tmp_path / "escape").exists()


def test_keys_that_resolve_to_the_same_file_are_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="map to the same backup file"):
        _copy_bucket(FakeS3({"a/b": b"one", "a//b": b"two"}), "b", tmp_path / "b")


def test_a_download_that_is_not_the_listed_size_fails(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="is 3 bytes, not 9"):
        _copy_bucket(FakeS3({"a": b"abc"}, listed_size={"a": 9}), "b", tmp_path / "b")


def test_the_command_is_registered_and_has_no_passphrase_option() -> None:
    # Rich styles option names with ANSI codes in CI; render plain, and strip what is left.
    result = CliRunner().invoke(
        app, ["infra", "backup", "--help"], env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    )
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    assert result.exit_code == 0
    assert "--output" in output and "OLF_BACKUP_PASSPHRASE" in output
    assert "--passphrase" not in output
