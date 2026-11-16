from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

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
