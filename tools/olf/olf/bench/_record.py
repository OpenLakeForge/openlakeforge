"""The machine-readable record a benchmark run leaves behind.

A run is only as good as its failures: a step that raised is recorded as
`failed` with its elapsed time and error, the run's status becomes `failed`,
and nothing after it runs. A reader never has to tell a stopped run from a
fast one by the numbers alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import olf

SCHEMA_VERSION = 1
# Variables `olf.deployment.local.config` reads to replace a pinned image or base image.
IMAGE_OVERRIDE_ENV = (
    "PROJECT_CODE_PYTHON_BASE_IMAGE",
    "PROJECT_CODE_IMAGE_REPOSITORY",
    "PROJECT_CODE_IMAGE_TAG",
    "SUPERSET_BASE_IMAGE",
)


class BenchError(RuntimeError):
    """A benchmark step failed, or its result did not validate."""


@dataclass
class Measurement:
    name: str
    status: str
    seconds: float | None = None
    started_at: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class BenchRecord:
    def __init__(
        self,
        scenario: str,
        *,
        environment: Mapping[str, Any],
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.scenario = scenario
        self.environment = dict(environment)
        self.measurements: list[Measurement] = []
        self._clock = clock
        self._wall_clock = wall_clock
        self.started_at = wall_clock()

    @property
    def status(self) -> str:
        return "failed" if any(item.status == "failed" for item in self.measurements) else "ok"

    @contextmanager
    def timed(self, name: str, **detail: Any) -> Iterator[dict[str, Any]]:
        """Time the block; the yielded dict is the measurement's detail, filled in by the block."""
        measurement = Measurement(name=name, status="running", started_at=self._wall_clock(), detail=dict(detail))
        self.measurements.append(measurement)
        start = self._clock()
        try:
            yield measurement.detail
        except BaseException as exc:
            measurement.status = "failed"
            measurement.error = str(exc) or type(exc).__name__
            raise
        else:
            measurement.status = "ok"
        finally:
            measurement.seconds = round(self._clock() - start, 3)

    def fact(self, name: str, detail: Mapping[str, Any]) -> None:
        """Record an untimed observation (a census, a footprint snapshot)."""
        self.measurements.append(
            Measurement(name=name, status="ok", started_at=self._wall_clock(), detail=dict(detail))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scenario": self.scenario,
            "status": self.status,
            "started_at": self.started_at,
            "environment": self.environment,
            "measurements": [asdict(item) for item in self.measurements],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def environment(repo_root: Path, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What a reader needs to reproduce or discount the numbers: host, toolchain, pins, overrides."""
    env = os.environ if environ is None else environ
    catalog = repo_root / "release" / "component-catalog.yaml"
    cluster_config = env.get("CLUSTER_CONFIG", "")
    return {
        "olf_version": olf.__version__,
        "git_commit": _output(["git", "-C", str(repo_root), "rev-parse", "HEAD"]),
        # A local edit to the deploy code changes what is measured; name the files, not just the fact.
        "git_modified": sorted((_output(["git", "-C", str(repo_root), "diff", "--name-only", "HEAD"]) or "").split()),
        "component_catalog_sha256": _sha256(catalog) if catalog.is_file() else None,
        "host": {
            "machine": platform.machine(),
            "kernel": platform.release(),
            "cpus": os.cpu_count(),
            "memory_bytes": _meminfo_total(),
            "cgroup": "v2" if Path("/sys/fs/cgroup/cgroup.controllers").exists() else "v1",
        },
        "docker_server_version": _output(["docker", "version", "--format", "{{.Server.Version}}"]),
        # A kind config other than the repository's changes what is measured; say so in the result.
        "cluster_config_override": (
            {"path": cluster_config, "sha256": _sha256(Path(cluster_config))} if cluster_config else None
        ),
        "image_overrides": {name: env[name] for name in IMAGE_OVERRIDE_ENV if env.get(name)},
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _meminfo_total() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _output(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None
