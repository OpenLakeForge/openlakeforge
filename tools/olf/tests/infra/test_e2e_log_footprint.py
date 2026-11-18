from __future__ import annotations

import gzip

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
