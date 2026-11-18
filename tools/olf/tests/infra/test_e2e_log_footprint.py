from __future__ import annotations

import gzip
import subprocess
from types import SimpleNamespace
from typing import Any, cast

import pytest

from olf.infra.e2e import _log_footprint as fp
from olf.infra.e2e._shell import E2EError


def _usage(cpu_ns: int, ws: int = 10) -> dict[str, fp.Usage]:
    return {"fluent-bit": fp.Usage(cpu_ns, ws, ws // 2)}


def test_cpu_is_the_mean_and_the_busiest_interval() -> None:
    samples = [
        fp.Sample(0.0, _usage(0, 100), 0),
        fp.Sample(5.0, _usage(50_000_000, 300), 1000),
        fp.Sample(10.0, _usage(60_000_000, 200), 500),
    ]

    stats = fp.summarize_phase("p", 10, samples)

    assert stats.cpu_millicores["fluent-bit"] == pytest.approx(6.0)
    assert stats.cpu_peak_millicores["fluent-bit"] == pytest.approx(10.0)
    assert stats.working_set_peak_bytes["fluent-bit"] == 300
    assert stats.spool_peak_bytes == 1000


def test_a_phase_without_a_probe_has_no_spool_figure() -> None:
    samples = [fp.Sample(0.0, _usage(0), None), fp.Sample(1.0, _usage(1), None)]

    assert fp.summarize_phase("p", 0, samples).spool_peak_bytes is None


def test_one_sample_cannot_give_a_rate() -> None:
    with pytest.raises(E2EError, match="fewer than two"):
        fp.summarize_phase("p", 0, [fp.Sample(0.0, _usage(0), None)])


def test_usage_is_read_for_the_named_pod_only() -> None:
    summary = {
        "pods": [
            {"podRef": {"name": "other", "namespace": "olf-system"}, "containers": []},
            {
                "podRef": {"name": "lc-1", "namespace": "olf-system"},
                "containers": [
                    {
                        "name": "fluent-bit",
                        "cpu": {"usageCoreNanoSeconds": 7},
                        "memory": {"workingSetBytes": 9, "rssBytes": 4},
                    }
                ],
            },
        ]
    }

    assert fp.container_usage(summary, "olf-system", "lc-1") == {"fluent-bit": fp.Usage(7, 9, 4)}
    with pytest.raises(E2EError, match="no pod"):
        fp.container_usage(summary, "olf-system", "missing")


@pytest.mark.parametrize(("text", "expected"), [("256Mi", 256 * 1024**2), ("512M", 512 * 1000**2), ("64", 64)])
def test_quantities_are_read_in_bytes(text: str, expected: int) -> None:
    assert fp.parse_size(text) == expected


def test_the_archive_scan_counts_lines_per_token_and_bytes() -> None:
    body = (
        b'{"message":"a-1 line 1 ERROR x"}\n{"message":"a-1 line 2 ERROR x"}\n'
        b'{"message":"b-2 line 1 ERROR x"}\n{"message":"noise"}\n'
    )
    gz = gzip.compress(body)

    stats = fp.archive_scan([(len(gz), gz), (len(gz), gz)], ["a-1", "b-2"])

    assert stats.seen == {"a-1": {1, 2}, "b-2": {1}}
    assert (stats.objects, stats.records, stats.uncompressed_bytes) == (2, 8, 2 * len(body))
    assert stats.compressed_bytes == 2 * len(gz)


def test_the_emitter_writes_the_rate_for_the_duration() -> None:
    script = fp.emit_script("tok", 100, 90)

    assert "-lt 90" in script
    assert "i+100-1" in script
    assert "-v t=tok" in script
    # Paced against the start time, so the time a batch takes does not lower the rate.
    assert "start=$(date +%s)" in script
    assert "sleep $((start+k-now))" in script
    assert "sleep 1;" not in script


def test_the_emitter_rate_is_paced_against_the_clock() -> None:
    script = fp.emit_script("tok", 3, 2)
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True, timeout=30)
    numbers = [int(line.split()[2]) for line in result.stderr.splitlines()]

    assert numbers == [1, 2, 3, 4, 5, 6]


def test_an_emitter_without_a_pod_yet_is_still_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp, "kubectl", lambda *_a, **_k: "")

    assert fp._emitter_running(cast(Any, SimpleNamespace(namespace="ns")), "job") is False  # noqa: SLF001


def test_a_failed_emitter_pod_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp, "kubectl", lambda *_a, **_k: "Pending Failed")

    with pytest.raises(E2EError):
        fp._emitter_running(cast(Any, SimpleNamespace(namespace="ns")), "job")  # noqa: SLF001


def _observation(stage: str, size: int, *, unreadable: bool = False) -> Any:
    return SimpleNamespace(stage=stage, size_bytes=size, unreadable=unreadable)


def test_an_unreadable_spool_is_unknown_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    target = fp.Target(pod={"status": {"podIP": "10.0.0.1"}, "spec": {"nodeName": "n"}}, stage="dev", api_pod="api")
    cfg = cast(Any, SimpleNamespace(platform_namespace="olf-system"))
    monkeypatch.setattr(fp, "kubectl", lambda *_a, **_k: "{}")

    monkeypatch.setattr(fp, "parse_spool_report", lambda _b: [_observation("dev", 0, unreadable=True)])
    assert fp._spool_bytes(cfg, target) is None  # noqa: SLF001

    monkeypatch.setattr(fp, "parse_spool_report", lambda _b: [_observation("dev", 5), _observation("prod", 7)])
    assert fp._spool_bytes(cfg, target) == 5  # noqa: SLF001


def test_an_emitter_that_never_starts_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[list[str]] = []
    monkeypatch.setattr(fp, "_start_emitter", lambda *_a: ("olf-e2e-log-x", "tok"))

    def never_runs(*_a: Any, **_k: Any) -> None:
        raise E2EError("timed out waiting for olf-e2e-log-x to run")

    monkeypatch.setattr(fp.collector, "_wait_until", never_runs)
    monkeypatch.setattr(fp, "kubectl", lambda _cfg, args, **_k: deleted.append(args) or "")
    target = fp.Target(pod={"spec": {"nodeName": "n"}}, stage="dev", api_pod=None)

    with pytest.raises(E2EError):
        fp._measure(cast(Any, SimpleNamespace(namespace="olf-dev")), target, fp.SIZES[0])  # noqa: SLF001

    assert ["delete", "job", "olf-e2e-log-x", "-n", "olf-dev", "--ignore-not-found", "--wait=false"] in deleted
