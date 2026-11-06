from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from olf.infra.e2e import _durability as durability
from olf.infra.e2e._shell import E2EError

SECRET = "s3cr3t-value-0123456789abcdef"


def _sources(**overrides: Any) -> durability.Sources:
    state: dict[str, Any] = {
        "tables": lambda: {
            "gold.orders": {"rows": 100, "snapshot_id": "111"},
            "silver.orders": {"rows": 120, "snapshot_id": "222"},
        },
        "dagster_runs": lambda: ["run-a", "run-b"],
        "dashboards": lambda: ["uuid-1"],
        "entities": lambda: ["sales", "svc.db.gold.orders"],
        "search_hits": lambda: 7,
        "activation": lambda: {"activations/dev/ACTIVE.json": "sha256:abc"},
        "objects": lambda: {"gold-bucket/a.parquet": 10, "ops/ACTIVE.json": 5},
    }
    return durability.Sources(**{**state, **overrides})


def test_an_unchanged_stage_verifies_clean_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    durability.write_baseline(durability.capture(_sources(), stage="dev"), path, {})

    assert durability.verify(durability.load_baseline(path), _sources()) == []


def test_new_state_is_not_a_difference() -> None:
    baseline = durability.capture(_sources(), stage="dev")

    later = _sources(dagster_runs=lambda: ["run-a", "run-b", "run-c"], objects=lambda: {**_sources().objects(), "x": 1})

    assert durability.verify(baseline, later) == []


def test_a_removed_run_and_a_changed_snapshot_are_both_reported() -> None:
    baseline = durability.capture(_sources(), stage="dev")
    after = _sources(
        dagster_runs=lambda: ["run-a"],
        tables=lambda: {
            "gold.orders": {"rows": 100, "snapshot_id": "999"},
            "silver.orders": {"rows": 120, "snapshot_id": "222"},
        },
    )

    assert durability.verify(baseline, after) == [
        "table gold.orders changed its current snapshot: 111 before, 999 now",
        "Dagster run run-b is missing",
    ]


def test_every_kind_of_loss_is_reported() -> None:
    baseline = durability.capture(_sources(), stage="dev")
    after = _sources(
        tables=lambda: {"silver.orders": {"rows": 5, "snapshot_id": "222"}},
        dashboards=lambda: [],
        entities=lambda: ["sales"],
        search_hits=lambda: 0,
        activation=lambda: {},
        objects=lambda: {"gold-bucket/a.parquet": 11},
    )

    assert durability.verify(baseline, after) == [
        "table gold.orders is missing",
        "table silver.orders has fewer rows: 120 before, 5 now",
        "Superset dashboard uuid-1 is missing",
        "OpenMetadata entity svc.db.gold.orders is missing",
        "OpenMetadata search finds nothing; it found 7 hits before",
        "activation pointer activations/dev/ACTIVE.json was sha256:abc, now None",
        "object gold-bucket/a.parquet changed size: 10 bytes before, 11 now",
        "object ops/ACTIVE.json is missing",
    ]


def test_a_source_that_cannot_be_read_is_a_difference_not_a_crash() -> None:
    def unreachable() -> list[str]:
        raise E2EError("Superset endpoint did not become reachable.")

    baseline = durability.capture(_sources(), stage="dev")

    assert durability.verify(baseline, _sources(dashboards=unreachable)) == [
        "the stage could not be read: Superset endpoint did not become reachable."
    ]


def test_a_capture_that_exposes_a_secret_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    leaking = _sources(entities=lambda: [f"svc.{SECRET}"])

    with pytest.raises(E2EError) as raised:
        durability.write_baseline(durability.capture(leaking, stage="dev"), path, {"ns/secret/key": SECRET})

    assert "the durability baseline contains the value of Secret ns/secret/key" in str(raised.value)
    assert SECRET not in str(raised.value)
    assert not path.exists()


def test_an_unreadable_or_foreign_file_is_not_a_baseline(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text('{"version": 99}')

    with pytest.raises(E2EError, match="cannot read the durability baseline"):
        durability.load_baseline(tmp_path / "missing.json")
    with pytest.raises(E2EError, match="not a version 1 durability baseline"):
        durability.load_baseline(other)
