from __future__ import annotations

import pytest
from openlakeforge_services.dagster import DagsterRun

from olf.infra.e2e import _silver_serialization as serialization
from olf.infra.e2e._shell import E2EError

TAG = serialization.SILVER_DOMAIN_TAG


def _run(job: str, started: float | None, ended: float | None, *, domain: str | None = "sales") -> DagsterRun:
    return DagsterRun(
        run_id=f"run-{job}",
        job_name=job,
        status="SUCCESS",
        created_at=0.0,
        started_at=started,
        ended_at=ended,
        tags={TAG: domain} if domain else {},
    )


def test_runs_that_start_after_the_other_ended_are_serial() -> None:
    serialization.assert_serialized([_run("b", 20.0, 30.0), _run("a", 10.0, 20.0)], domain="sales")


def test_overlapping_runs_are_refused() -> None:
    with pytest.raises(E2EError, match="overlapped"):
        serialization.assert_serialized([_run("a", 10.0, 25.0), _run("b", 20.0, 30.0)], domain="sales")


def test_a_run_without_the_domain_tag_is_refused() -> None:
    with pytest.raises(E2EError, match="lacks the"):
        serialization.assert_serialized([_run("a", 10.0, 20.0, domain=None), _run("b", 20.0, 30.0)], domain="sales")


def test_the_check_uses_the_tag_the_runtime_sets() -> None:
    pytest.importorskip("dagster")
    from openlakeforge_runtime import product_dagster

    assert TAG == product_dagster.SILVER_DOMAIN_TAG
