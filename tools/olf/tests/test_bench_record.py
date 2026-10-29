from __future__ import annotations

import json
from pathlib import Path

import pytest

from olf.bench import BenchError, BenchRecord, environment


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_a_completed_block_is_timed_and_keeps_what_it_recorded() -> None:
    clock = _Clock()
    record = BenchRecord("etl", environment={}, clock=clock, wall_clock=clock)

    with record.timed("initial_order_revenue", scale=10) as detail:
        clock.now += 12.5
        detail["rows"] = 42

    (measurement,) = record.measurements
    assert (measurement.status, measurement.seconds) == ("ok", 12.5)
    assert measurement.detail == {"scale": 10, "rows": 42}
    assert record.status == "ok"


def test_a_failed_block_fails_the_run_and_keeps_its_error() -> None:
    clock = _Clock()
    record = BenchRecord("etl", environment={}, clock=clock, wall_clock=clock)

    with pytest.raises(BenchError):
        with record.timed("deploy_platform"):
            clock.now += 3
            raise BenchError("helm timed out")

    (measurement,) = record.measurements
    assert (measurement.status, measurement.seconds, measurement.error) == ("failed", 3.0, "helm timed out")
    assert record.status == "failed"


def test_the_written_result_carries_status_environment_and_measurements(tmp_path: Path) -> None:
    record = BenchRecord("footprint", environment={"host": {"cpus": 4}})
    record.fact("installed_footprint", {"pods": 31})

    path = record.write(tmp_path / "out" / "result.json")

    document = json.loads(path.read_text())
    assert document["scenario"] == "footprint"
    assert document["status"] == "ok"
    assert document["environment"] == {"host": {"cpus": 4}}
    assert document["measurements"][0]["detail"] == {"pods": 31}


def test_environment_records_the_catalog_pin_and_a_kind_config_override(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    override = tmp_path / "kind.yaml"
    override.write_text("kind: Cluster\n")

    facts = environment(repo, environ={"CLUSTER_CONFIG": str(override), "PROJECT_CODE_PYTHON_BASE_IMAGE": "py:ca"})

    assert len(facts["component_catalog_sha256"]) == 64
    assert facts["cluster_config_override"]["path"] == str(override)
    assert facts["image_overrides"] == {"PROJECT_CODE_PYTHON_BASE_IMAGE": "py:ca"}
    assert facts["host"]["cpus"]


def test_environment_without_an_override_says_so(tmp_path: Path) -> None:
    facts = environment(tmp_path, environ={})

    assert facts["cluster_config_override"] is None
    assert facts["component_catalog_sha256"] is None
    assert facts["image_overrides"] == {}
    assert facts["git_modified"] == []
