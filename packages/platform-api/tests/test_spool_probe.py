from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from openlakeforge_platform_api import spool_probe

NOW = 1_800_000_000.0


def _file(path: Path, size: int, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (NOW - age_seconds, NOW - age_seconds))


def test_the_oldest_file_and_the_total_size_of_each_stage(tmp_path: Path) -> None:
    _file(tmp_path / "spool-dev" / "ops" / "olf-dev.2026-11-16" / "a", 100, 120)
    _file(tmp_path / "spool-dev" / "ops" / "olf-dev.2026-11-16" / "b", 50, 900)
    _file(tmp_path / "spool-prod" / "c", 7, 5)

    report = spool_probe.build_report(tmp_path, {"dev": "spool-dev", "prod": "spool-prod"}, 512_000_000, now=NOW)

    assert report["stages"]["dev"] == {"oldest_file_age_seconds": 900, "size_bytes": 150}
    assert report["stages"]["prod"] == {"oldest_file_age_seconds": 5, "size_bytes": 7}


def test_an_empty_or_missing_spool_has_no_oldest_file(tmp_path: Path) -> None:
    (tmp_path / "spool-dev").mkdir()

    report = spool_probe.build_report(tmp_path, {"dev": "spool-dev", "uat": "spool-uat"}, 1, now=NOW)

    assert report["stages"]["dev"] == {"oldest_file_age_seconds": None, "size_bytes": 0}
    assert report["stages"]["uat"] == {"oldest_file_age_seconds": None, "size_bytes": 0}


def test_quarantined_files_are_not_the_spool(tmp_path: Path) -> None:
    _file(tmp_path / "spool-dev" / "live", 10, 60)
    _file(tmp_path / "spool-dev" / spool_probe.QUARANTINE_DIR / "gave-up", 999, 86_400)

    report = spool_probe.build_report(tmp_path, {"dev": "spool-dev"}, 1, now=NOW)

    assert report["stages"]["dev"] == {"oldest_file_age_seconds": 60, "size_bytes": 10}


def test_a_symlink_is_not_followed_out_of_the_spool(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _file(outside / "big", 1000, 99_999)
    (tmp_path / "spool-dev").mkdir()
    (tmp_path / "spool-dev" / "link").symlink_to(outside)

    report = spool_probe.build_report(tmp_path, {"dev": "spool-dev"}, 1, now=NOW)

    assert report["stages"]["dev"]["size_bytes"] == 0


def test_a_report_parses_into_observations(tmp_path: Path) -> None:
    _file(tmp_path / "spool-dev" / "a", 100, 600)
    report = spool_probe.build_report(tmp_path, {"dev": "spool-dev", "uat": "spool-uat"}, 512_000_000, now=NOW)

    by_stage = {o.stage: o for o in spool_probe.parse_report(json.loads(json.dumps(report)))}

    assert by_stage["dev"].oldest_file_age == timedelta(seconds=600)
    assert by_stage["dev"].size_bytes == 100
    assert by_stage["dev"].limit_bytes == 512_000_000
    assert by_stage["uat"].oldest_file_age is None


@pytest.mark.parametrize("payload", [{}, {"observed_at": "2026-11-16T00:00:00+00:00", "limit_bytes": 1, "stages": []}])
def test_a_malformed_report_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="malformed"):
        spool_probe.parse_report(payload)


def test_a_report_without_a_zone_is_refused() -> None:
    payload = {
        "observed_at": "2026-11-16T00:00:00",
        "limit_bytes": 1,
        "stages": {"dev": {"oldest_file_age_seconds": None, "size_bytes": 0}},
    }

    with pytest.raises(ValueError, match="timezone"):
        spool_probe.parse_report(payload)


@pytest.mark.parametrize(("text", "expected"), [("512M", 512_000_000), ("2G", 2_000_000_000), ("64K", 64_000)])
def test_sizes_read_as_fluent_bit_writes_them(text: str, expected: int) -> None:
    assert spool_probe.parse_size(text) == expected


@pytest.mark.parametrize("text", ["0", "0M", "512", "512MB", ""])
def test_an_unbounded_or_unitless_limit_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        spool_probe.parse_size(text)


def test_the_server_answers_the_spool_path_only(tmp_path: Path) -> None:
    _file(tmp_path / "spool-dev" / "a", 10, 60)
    server = ThreadingHTTPServer(("127.0.0.1", 0), spool_probe._handler(tmp_path, {"dev": "spool-dev"}, 1000))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + spool_probe.PATH, timeout=5) as response:  # noqa: S310
            observed = spool_probe.parse_report(json.load(response))
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/", timeout=5)  # noqa: S310
    finally:
        server.shutdown()
        server.server_close()

    assert [(o.stage, o.size_bytes) for o in observed] == [("dev", 10)]
    assert missing.value.code == 404
