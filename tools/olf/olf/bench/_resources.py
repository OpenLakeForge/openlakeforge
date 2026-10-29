"""Cluster resource observations for a local kind deployment.

CPU and memory are read per kind node container from `docker stats`, so they
include the kubelet, containerd and the control plane: the cost a team's
machine actually pays, not just the pods' requests. A sampler thread keeps a
series that later windows (an idle period, one pipeline step) are cut from.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from olf.bench._record import BenchError

_UNITS = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "kb": 1000, "mb": 1000**2, "gb": 1000**3}
# Where a kind node keeps image layers and PersistentVolumes (local-path provisioner).
NODE_STORAGE_PATHS = ("/var/lib/containerd", "/var/local-path-provisioner")

Runner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class Sample:
    at: float
    cpu_cores: float
    memory_bytes: int


def run_command(argv: Sequence[str]) -> str:
    result = subprocess.run(list(argv), capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise BenchError(f"{' '.join(argv)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def kind_nodes(cluster_name: str, *, run: Runner = run_command) -> list[str]:
    out = run(["docker", "ps", "--filter", f"label=io.x-k8s.kind.cluster={cluster_name}", "--format", "{{.Names}}"])
    nodes = sorted(line.strip() for line in out.splitlines() if line.strip())
    if not nodes:
        raise BenchError(f"no running kind nodes for cluster {cluster_name!r}")
    return nodes


def parse_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", value)
    if not match or match.group(2).lower() not in _UNITS:
        raise BenchError(f"unparseable size {value!r}")
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def node_usage(nodes: Sequence[str], *, run: Runner = run_command, now: Callable[[], float] = time.time) -> Sample:
    """One summed reading over the nodes; 1.0 CPU core is 100% in `docker stats`."""
    out = run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *nodes])
    cpu = 0.0
    memory = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cpu += float(row["CPUPerc"].rstrip("%")) / 100
        memory += parse_bytes(row["MemUsage"].split("/")[0])
    return Sample(at=now(), cpu_cores=round(cpu, 3), memory_bytes=memory)


def summarize(samples: Sequence[Sample]) -> dict[str, Any]:
    if not samples:
        return {"samples": 0}
    cpu = sorted(sample.cpu_cores for sample in samples)
    memory = sorted(sample.memory_bytes for sample in samples)

    def p95(values: list[Any]) -> Any:
        return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]

    return {
        "samples": len(samples),
        "cpu_cores_mean": round(sum(cpu) / len(cpu), 3),
        "cpu_cores_p95": p95(cpu),
        "cpu_cores_max": cpu[-1],
        "memory_bytes_mean": int(sum(memory) / len(memory)),
        "memory_bytes_p95": p95(memory),
        "memory_bytes_max": memory[-1],
    }


class Sampler:
    """Samples node usage on a background thread until stopped."""

    def __init__(
        self, nodes: Sequence[str], *, interval: float = 5.0, read: Callable[[], Sample] | None = None
    ) -> None:
        self._read = read or (lambda: node_usage(nodes))
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="olf-bench-sampler", daemon=True)
        self.samples: list[Sample] = []
        self.errors = 0

    def __enter__(self) -> Sampler:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval + 60)

    def window(self, start: float, end: float) -> dict[str, Any]:
        return summarize([sample for sample in list(self.samples) if start <= sample.at <= end])

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append(self._read())
            except (BenchError, subprocess.TimeoutExpired, ValueError, KeyError):
                self.errors += 1
            self._stop.wait(self._interval)


def pod_census(pods_json: str) -> dict[str, Any]:
    """Pods per namespace and phase, from `kubectl get pods -A -o json`."""
    items = json.loads(pods_json).get("items", [])
    by_namespace: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    containers = 0
    for item in items:
        namespace = item.get("metadata", {}).get("namespace", "")
        phase = item.get("status", {}).get("phase", "Unknown")
        by_namespace[namespace] = by_namespace.get(namespace, 0) + 1
        by_phase[phase] = by_phase.get(phase, 0) + 1
        if phase == "Running":
            containers += len(item.get("spec", {}).get("containers", []))
    return {
        "pods": len(items),
        "running_containers": containers,
        "by_phase": dict(sorted(by_phase.items())),
        "by_namespace": dict(sorted(by_namespace.items())),
    }


def node_storage(nodes: Sequence[str], *, run: Runner = run_command) -> dict[str, Any]:
    """Bytes under each node's image store and PersistentVolume root."""
    totals = dict.fromkeys(NODE_STORAGE_PATHS, 0)
    for node in nodes:
        # A node without PersistentVolumes has no provisioner root; du still sizes the rest.
        out = run(["docker", "exec", node, "sh", "-c", f"du -sxb {' '.join(NODE_STORAGE_PATHS)} 2>/dev/null || true"])
        for line in out.splitlines():
            size, _, path = line.partition("\t")
            if path in totals:
                totals[path] += int(size)
    return {"nodes": len(nodes), "bytes_by_path": totals, "bytes_total": sum(totals.values())}
