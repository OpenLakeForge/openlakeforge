from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from openlakeforge_core import HealthState, SpoolObservation

from olf.infra.e2e import _log_collector, _log_delivery
from olf.infra.e2e._shell import E2EError

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
LIMIT = 512_000_000


def _policy() -> dict:
    return {
        "metadata": {
            "name": "olf-flows-log-collector",
            "namespace": "olf-system",
            "uid": "u",
            "resourceVersion": "9",
            "managedFields": [{}],
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"},
        },
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "log-collector"}},
            "egress": [
                {"to": [{"podSelector": {}}], "ports": [{"port": "8333", "protocol": "TCP"}]},
                {"to": [{"ipBlock": {"cidr": "10.0.0.1/32"}}], "ports": [{"port": "443"}, {"port": "6443"}]},
                {"ports": [{"port": 53, "protocol": "UDP"}]},
            ],
        },
        "status": {},
    }


def test_the_cut_removes_the_s3_rule_and_keeps_the_rest() -> None:
    cut = _log_delivery.without_egress_port(_policy(), 8333)

    assert [rule["ports"][0]["port"] for rule in cut["spec"]["egress"]] == ["443", 53]
    assert cut["spec"]["podSelector"] == _policy()["spec"]["podSelector"]


def test_the_cut_leaves_the_original_untouched() -> None:
    original = _policy()

    _log_delivery.without_egress_port(original, 8333)

    assert original == _policy()


def test_a_replaceable_policy_carries_no_server_fields() -> None:
    result = _log_delivery.replaceable(_policy())

    assert set(result["metadata"]) == {"name", "namespace", "annotations"}
    assert result["metadata"]["annotations"] == {}
    assert "status" not in result


def test_engine_failure_counters_of_the_s3_output_are_summed() -> None:
    metrics = "\n".join(
        [
            'fluentbit_output_errors_total{name="s3.0"} 0',
            'fluentbit_output_retries_failed_total{name="s3.1"} 2',
            'fluentbit_output_dropped_records_total{name="s3.2"} 3',
            'fluentbit_output_errors_total{name="stdout.0"} 9',
            'fluentbit_input_records_total{name="tail.0"} 100',
        ]
    )

    assert _log_delivery.engine_failures(metrics) == 5
    assert _log_delivery.engine_failures('fluentbit_output_errors_total{name="s3.0"} 0\n') == 0


def _spool(stage: str, age: timedelta | None, size: int = 0) -> SpoolObservation:
    return SpoolObservation(stage, NOW, age, size, LIMIT)


def test_the_stage_signals_take_the_worst_collector_pod_and_ignore_other_stages() -> None:
    pods = [
        _spool("dev", timedelta(minutes=1)),
        _spool("dev", timedelta(minutes=12)),
        _spool("prod", timedelta(hours=2)),
    ]

    signals = _log_delivery.stage_signals("dev", pods, timedelta(minutes=20), now=NOW)

    assert signals["spool-age"].state is HealthState.WARNING
    assert signals["spool-size"].state is HealthState.HEALTHY
    assert signals["newest-object"].state is HealthState.WARNING


def test_a_stage_no_collector_pod_reports_is_an_error_not_a_healthy_stage() -> None:
    with pytest.raises(E2EError, match="no collector pod"):
        _log_delivery.stage_signals("dev", [_spool("prod", None)], None, now=NOW)


def test_loss_is_described_as_ranges() -> None:
    assert _log_delivery.describe_loss({1, 2, 3, 4, 5, 9, 10}, 10) == "3 of 10 lines missing: 6-8"
    assert _log_delivery.describe_loss(set(range(1, 4)), 3) == "all 3 lines archived"


def test_the_fill_script_writes_numbered_lines_the_reader_can_count() -> None:
    script = _log_delivery.fill_script("tok", 2 * _log_delivery.FILL_LINES_PER_SECOND).replace("sleep 1", "true")

    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True)

    numbers = _log_collector.line_numbers(result.stderr.splitlines(), "tok")
    assert numbers == list(range(1, 2 * _log_delivery.FILL_LINES_PER_SECOND + 1))
    assert len(result.stderr.splitlines()[0]) > _log_delivery.FILL_LINE_PADDING


def test_the_probe_is_fetched_from_inside_a_pod_with_the_images_python() -> None:
    args = _log_delivery.exec_get_args("olf-system", "platform-api-x", "http://10.0.0.7:2021/spool")

    assert args[:5] == ["exec", "platform-api-x", "-n", "olf-system", "--"]
    assert args[5:7] == ["python", "-c"]
    assert args[-1] == "http://10.0.0.7:2021/spool"
    assert "-c" not in args[2:5]


def test_the_engine_metrics_are_fetched_from_the_probe_container_over_loopback() -> None:
    args = _log_delivery.exec_get_args(
        "olf-system", "log-collector-a", "http://127.0.0.1:2020/x", container="spool-probe"
    )

    assert args[:6] == ["exec", "log-collector-a", "-n", "olf-system", "-c", "spool-probe"]


def test_a_collector_pod_without_an_address_is_an_error() -> None:
    pods = {"items": [{"metadata": {"name": "a"}, "status": {"podIP": "10.0.0.1"}}, {"metadata": {"name": "b"}}]}

    with pytest.raises(E2EError, match="collector pod b has no IP"):
        _log_delivery.collector_addresses(pods)
    assert _log_delivery.collector_addresses({"items": pods["items"][:1]}) == [("a", "10.0.0.1")]


def test_files_created_after_the_restore_do_not_hold_the_drain() -> None:
    at_restore = {"worker": {"a": 10, "b": 20}, "worker2": {"c": 30}}
    now = {"worker": {"d": 5}, "worker2": {"e": 7}}

    assert _log_delivery.undelivered(at_restore, now) == {}


def test_outage_files_still_spooled_are_pending_per_node() -> None:
    at_restore = {"worker": {"a": 10, "b": 20}, "worker2": {"c": 30}}
    now = {"worker": {"b": 25, "d": 5}, "worker2": {}}

    assert _log_delivery.undelivered(at_restore, now) == {"worker": ["b"]}


def test_a_node_that_no_longer_reports_keeps_its_outage_files_pending() -> None:
    assert _log_delivery.undelivered({"worker": {"a": 1}}, {}) == {"worker": ["a"]}


def test_the_spool_listing_skips_the_quarantine_and_reads_a_missing_spool_as_empty(tmp_path: Path) -> None:
    spool = tmp_path / "spool-dev"
    (spool / "bucket" / "stream").mkdir(parents=True)
    (spool / "bucket" / "stream" / "file1").write_bytes(b"x" * 12)
    (spool / "quarantine").mkdir()
    (spool / "quarantine" / "old").write_bytes(b"y" * 99)

    def listing(directory: str) -> dict[str, int]:
        result = subprocess.run(
            [sys.executable, "-c", _log_delivery._LIST_SPOOL, directory], capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)

    assert listing(str(spool)) == {"bucket/stream/file1": 12}
    assert listing(str(tmp_path / "absent")) == {}


def _drain(monkeypatch: pytest.MonkeyPatch, *spools: dict[str, dict[str, int]]) -> None:
    healthy = SimpleNamespace(state=HealthState.HEALTHY, reason="ok")
    answers = iter(spools)
    monkeypatch.setattr(_log_delivery, "spool_files", lambda cfg: next(answers))
    monkeypatch.setattr(_log_delivery, "_signals", lambda cfg: {"spool-age": healthy, "newest-object": healthy})
    monkeypatch.setattr(_log_delivery.time, "sleep", lambda _s: None)


def test_the_drain_ends_when_the_outage_files_are_gone_though_new_ones_are_spooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _drain(monkeypatch, {"worker": {"a": 1, "new": 2}}, {"worker": {"new": 3, "newer": 1}})

    _log_delivery._wait_drained(SimpleNamespace(), {"worker": {"a": 1}}, 600)  # type: ignore[arg-type]


def test_a_drain_that_never_sends_the_outage_files_names_them(monkeypatch: pytest.MonkeyPatch) -> None:
    _drain(monkeypatch, *[{"worker": {"a": 1}}] * 10)
    ticks = iter(range(0, 1000, 100))
    monkeypatch.setattr(_log_delivery.time, "monotonic", lambda: float(next(ticks)))

    with pytest.raises(E2EError, match=r"outage files still spooled: \{'worker': \['a'\]\}"):
        _log_delivery._wait_drained(SimpleNamespace(), {"worker": {"a": 1}}, 250)  # type: ignore[arg-type]


def test_the_archive_is_read_until_two_reads_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_log_delivery.time, "sleep", lambda _s: None)
    reads = iter([{1}, {1, 2}, {1, 2, 3}, {1, 2, 3}, {9}])

    assert _log_delivery.await_stable(lambda: next(reads), settle_seconds=1, timeout_seconds=60) == {1, 2, 3}


def test_an_archive_that_never_stops_changing_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_log_delivery.time, "sleep", lambda _s: None)
    ticks = iter(range(100))
    monkeypatch.setattr(_log_delivery.time, "monotonic", lambda: next(ticks) * 10.0)
    counter = iter(range(100))

    with pytest.raises(E2EError, match="still changing"):
        _log_delivery.await_stable(lambda: {next(counter)}, settle_seconds=1, timeout_seconds=30)


@pytest.mark.parametrize(
    ("status", "state"),
    [({}, "running"), ({"active": 1}, "running"), ({"succeeded": 1}, "complete"), ({"failed": 1}, "failed")],
)
def test_a_jobs_state_comes_from_its_status(status: dict[str, int], state: str) -> None:
    assert _log_delivery.job_state({"status": status}) == state


def test_the_fill_timeout_grows_with_the_lines_to_write() -> None:
    per_second = _log_delivery.FILL_LINES_PER_SECOND

    assert _log_delivery.fill_timeout_seconds(100 * per_second) > _log_delivery.fill_timeout_seconds(10 * per_second)
    assert _log_delivery.fill_timeout_seconds(100 * per_second) >= 200


def _jobs(monkeypatch: pytest.MonkeyPatch, *statuses: dict[str, int]) -> None:
    answers = iter(json.dumps({"status": status}) for status in statuses)
    monkeypatch.setattr(_log_delivery, "kubectl", lambda cfg, args, **kw: next(answers))
    monkeypatch.setattr(_log_delivery.time, "sleep", lambda _s: None)


def test_the_drill_waits_for_the_emitter_to_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    _jobs(monkeypatch, {"active": 1}, {"active": 1}, {"succeeded": 1})

    _log_delivery._wait_job_complete(SimpleNamespace(namespace="olf-dev"), "fill", 600)  # type: ignore[arg-type]


def test_a_failed_emitter_is_an_error_not_a_smaller_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _jobs(monkeypatch, {"active": 1}, {"failed": 1})

    with pytest.raises(E2EError, match="failed"):
        _log_delivery._wait_job_complete(SimpleNamespace(namespace="olf-dev"), "fill", 600)  # type: ignore[arg-type]


def test_an_emitter_that_never_finishes_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _jobs(monkeypatch, *[{"active": 1}] * 10)
    ticks = iter(range(0, 1000, 100))
    monkeypatch.setattr(_log_delivery.time, "monotonic", lambda: float(next(ticks)))

    with pytest.raises(E2EError, match="had not finished"):
        _log_delivery._wait_job_complete(SimpleNamespace(namespace="olf-dev"), "fill", 250)  # type: ignore[arg-type]
