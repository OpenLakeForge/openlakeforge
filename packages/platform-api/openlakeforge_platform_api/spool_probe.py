"""The log collector's spool probe (ADR 0022, decision 2): the sidecar that measures the S3 output's `store_dir`.

It runs next to Fluent Bit in the collector pod, reads the shared state volume read-only and serves two numbers per
stage over HTTP: the age of the oldest file and the bytes held. It writes nothing and holds no credential. The
platform API reads it (`parse_report`) and classifies the numbers with `openlakeforge_core.archive_health`.

Age is the file's mtime. A spool file the plugin is still appending to looks young, so an outage is seen first by its
size, then by the files that filled up and stopped changing [T: confirmed by the failure-injection e2e].
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openlakeforge_core import SpoolObservation

PORT = 2021
PATH = "/spool"
# The plugin moves files whose retries are exhausted here; they have left the spool's size budget, so counting
# them would report a full spool for data the plugin already gave up on [T: directory name, ADR 0022].
QUARANTINE_DIR = "quarantine"
_UNITS = {"K": 1000, "M": 1000**2, "G": 1000**3}


def parse_size(text: str) -> int:
    """`512M` as Fluent Bit writes a size. The multiplier is 1000, the smaller reading, so a fraction errs high [T]."""
    match = re.fullmatch(r"([1-9][0-9]*)([KMG])", text)
    if match is None:
        raise ValueError(f"{text!r} is not a positive size such as 512M")
    return int(match.group(1)) * _UNITS[match.group(2)]


def scan_stage(directory: Path, *, now: float) -> tuple[float | None, int]:
    """The oldest file's age in seconds (None when there is none) and the bytes held under `directory`.

    Only a path that vanishes is tolerated: the plugin uploads and removes files while this runs, and a spool
    directory it has not created yet is an empty spool. Any other OSError (permissions, I/O) propagates, because
    skipping it would report a spool nobody could read as an empty one.
    """
    oldest: float | None = None
    size = 0
    pending = [directory]
    while pending:
        try:
            entries = list(pending.pop().iterdir())
        except FileNotFoundError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name != QUARANTINE_DIR:
                        pending.append(entry)
                    continue
                info = entry.stat()
            except FileNotFoundError:
                continue
            size += info.st_size
            age = max(0.0, now - info.st_mtime)
            oldest = age if oldest is None else max(oldest, age)
    return oldest, size


def build_report(root: Path, stages: Mapping[str, str], limit_bytes: int, *, now: float) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for stage, name in stages.items():
        try:
            oldest, size = scan_stage(root / name, now=now)
        except OSError:
            # Unreadable is not empty: the consumer reads it as unknown, never as a healthy spool.
            report[stage] = {"oldest_file_age_seconds": None, "size_bytes": 0, "unreadable": True}
        else:
            report[stage] = {"oldest_file_age_seconds": oldest, "size_bytes": size}
    return {
        "observed_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "limit_bytes": limit_bytes,
        "stages": report,
    }


def parse_report(payload: Mapping[str, Any]) -> list[SpoolObservation]:
    """The observations in a probe's answer; a malformed answer raises ValueError, never a guess."""
    try:
        observed_at = datetime.fromisoformat(str(payload["observed_at"]))
        limit = int(payload["limit_bytes"])
        return [
            SpoolObservation(
                stage=str(stage),
                observed_at=observed_at,
                oldest_file_age=None if age is None else timedelta(seconds=float(age)),
                size_bytes=int(values["size_bytes"]),
                limit_bytes=limit,
                unreadable=values.get("unreadable") is True,
            )
            for stage, values in payload["stages"].items()
            for age in [values["oldest_file_age_seconds"]]
        ]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("the spool report is malformed") from exc


def _handler(root: Path, stages: Mapping[str, str], limit_bytes: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            if self.path != PATH:
                self.send_error(404)
                return
            body = json.dumps(build_report(root, stages, limit_bytes, now=time.time())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the stdlib's signature
            pass

    return Handler


def serve(root: Path, stages: Mapping[str, str], limit_bytes: int, *, host: str = "0.0.0.0", port: int = PORT) -> None:  # noqa: S104
    ThreadingHTTPServer((host, port), _handler(root, stages, limit_bytes)).serve_forever()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the log collector's spool age and size.")
    parser.add_argument("--root", type=Path, required=True, help="the collector's state directory (read-only)")
    parser.add_argument("--limit", required=True, help="store_dir_limit_size of each output, such as 512M")
    parser.add_argument("--stage", action="append", required=True, metavar="STAGE=DIR", help="a stage and its spool")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    stages = dict(item.split("=", 1) for item in args.stage)
    serve(args.root, stages, parse_size(args.limit), port=args.port)


if __name__ == "__main__":
    main()
