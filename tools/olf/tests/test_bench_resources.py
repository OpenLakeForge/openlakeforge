from __future__ import annotations

import json
import time
from collections.abc import Sequence

import pytest

from olf.bench import BenchError
from olf.bench._resources import (
    Sample,
    Sampler,
    kind_nodes,
    node_storage,
    node_usage,
    parse_bytes,
    pod_census,
    summarize,
)


def test_parse_bytes_reads_docker_units() -> None:
    assert parse_bytes("1.5GiB") == int(1.5 * 1024**3)
    assert parse_bytes("512MiB ") == 512 * 1024**2
    assert parse_bytes("20kB") == 20_000
    with pytest.raises(BenchError):
        parse_bytes("lots")


def test_node_usage_sums_cpu_cores_and_memory_across_nodes() -> None:
    rows = [
        {"Name": "cp", "CPUPerc": "150.00%", "MemUsage": "2GiB / 15.7GiB"},
        {"Name": "w1", "CPUPerc": "25.50%", "MemUsage": "512MiB / 15.7GiB"},
    ]

    def run(argv: Sequence[str]) -> str:
        assert argv[:3] == ["docker", "stats", "--no-stream"]
        return "\n".join(json.dumps(row) for row in rows) + "\n"

    sample = node_usage(["cp", "w1"], run=run, now=lambda: 7.0)

    assert sample == Sample(at=7.0, cpu_cores=1.755, memory_bytes=2 * 1024**3 + 512 * 1024**2)


def test_kind_nodes_requires_a_running_cluster() -> None:
    assert kind_nodes("c", run=lambda _argv: "c-worker\nc-control-plane\n") == ["c-control-plane", "c-worker"]
    with pytest.raises(BenchError, match="no running kind nodes"):
        kind_nodes("c", run=lambda _argv: "")


def test_summarize_reports_mean_p95_and_max() -> None:
    samples = [Sample(at=float(i), cpu_cores=float(i), memory_bytes=i * 10) for i in range(1, 21)]

    summary = summarize(samples)

    assert summary["samples"] == 20
    assert summary["cpu_cores_mean"] == 10.5
    assert summary["cpu_cores_p95"] == 19.0
    assert summary["memory_bytes_max"] == 200
    assert summarize([]) == {"samples": 0}


def test_pod_census_counts_by_namespace_and_phase() -> None:
    pods = {
        "items": [
            {"metadata": {"namespace": "olf-system"}, "status": {"phase": "Running"}, "spec": {"containers": [{}, {}]}},
            {"metadata": {"namespace": "olf-dev"}, "status": {"phase": "Running"}, "spec": {"containers": [{}]}},
            {"metadata": {"namespace": "olf-dev"}, "status": {"phase": "Succeeded"}, "spec": {"containers": [{}]}},
        ]
    }

    census = pod_census(json.dumps(pods))

    assert census == {
        "pods": 3,
        "running_containers": 3,
        "by_phase": {"Running": 2, "Succeeded": 1},
        "by_namespace": {"olf-dev": 2, "olf-system": 1},
    }


def test_node_storage_sums_each_path_over_nodes() -> None:
    outputs = {
        "a": "100\t/var/lib/containerd\n7\t/var/local-path-provisioner\n",
        "b": "50\t/var/lib/containerd\n",
    }

    storage = node_storage(["a", "b"], run=lambda argv: outputs[argv[2]])

    assert storage["bytes_by_path"] == {"/var/lib/containerd": 150, "/var/local-path-provisioner": 7}
    assert storage["bytes_total"] == 157


def test_sampler_keeps_readings_and_counts_failed_ones() -> None:
    readings = iter(
        [Sample(at=1.0, cpu_cores=1.0, memory_bytes=1), None, Sample(at=3.0, cpu_cores=3.0, memory_bytes=3)]
    )

    def read() -> Sample:
        value = next(readings, None)
        if value is None:
            raise BenchError("docker stats failed")
        return value

    with Sampler([], interval=0.01, read=read) as sampler:
        deadline = time.monotonic() + 5
        while len(sampler.samples) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert [sample.at for sample in sampler.samples[:2]] == [1.0, 3.0]
    assert sampler.errors >= 1
    assert sampler.window(2.0, 4.0)["samples"] == 1
