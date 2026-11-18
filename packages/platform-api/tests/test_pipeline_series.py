"""Pipeline run series derived from Dagster reads, evaluated over a fixed window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterJob, DagsterRun

from openlakeforge_platform_api.pipeline_series import (
    PIPELINE_RUN_DURATION_SECONDS,
    PIPELINE_RUNS,
    RUN_CAP,
    PipelineSeries,
    read_pipeline_series,
)

END = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
STEP = timedelta(minutes=15)
JOB = "orders_pipeline"


class FakeDagster(DagsterClient):
    def __init__(self, runs: list[DagsterRun], jobs: tuple[str, ...] = (JOB,), *, broken: bool = False) -> None:
        super().__init__("http://dagster.invalid/graphql")
        self.runs, self.jobs, self.broken = runs, jobs, broken
        self.reads: list[tuple[float, int]] = []

    def list_jobs(self) -> list[DagsterJob]:
        if self.broken:
            raise ServiceClientError("PythonError: storage unavailable\n  Traceback ...")
        return [DagsterJob("loc", "repo", job, "") for job in self.jobs]

    def list_runs_created_after(self, created_after: float, *, limit: int) -> list[DagsterRun]:
        self.reads.append((created_after, limit))
        matching = [run for run in self.runs if run.created_at > created_after]
        return sorted(matching, key=lambda run: run.created_at, reverse=True)[:limit]


def _run(
    created: datetime, status: str = "SUCCESS", *, seconds: float | None = 120, job: str = JOB, started: bool = True
) -> DagsterRun:
    begin = created.timestamp() + 5 if started else None
    end = begin + seconds if begin is not None and seconds is not None else None
    return DagsterRun(f"{job}-{created.timestamp()}", job, status, created.timestamp(), begin, end)


def _read(client: DagsterClient | None, series: str = PIPELINE_RUNS, **window: timedelta) -> PipelineSeries:
    return read_pipeline_series(
        client, series, "dev", end=END, duration=window.get("duration", HOUR), step=window.get("step", STEP)
    )


def _values(result: PipelineSeries, **labels: str) -> list[float | None]:
    [line] = [line for line in result.lines if all(line.labels.get(key) == value for key, value in labels.items())]
    return [value for _, value in line.points]


def test_runs_are_counted_by_job_and_status_in_the_step_they_were_created_in() -> None:
    client = FakeDagster(
        [
            _run(END - timedelta(minutes=50)),
            _run(END - timedelta(minutes=40), "FAILURE"),
            _run(END - timedelta(minutes=35)),
            _run(END - timedelta(minutes=1), "STARTED", seconds=None),
            _run(END - timedelta(minutes=20), "CANCELING"),
        ]
    )

    result = _read(client)

    assert result.state == "available" and result.complete and result.covered_from is None
    assert [time for time, _ in result.lines[0].points] == [END - STEP * 3, END - STEP * 2, END - STEP, END]
    assert _values(result, status="succeeded") == [1, 1, 0, 0]
    assert _values(result, status="failed") == [0, 1, 0, 0]
    assert _values(result, status="canceled") == [0, 0, 1, 0]
    assert _values(result, status="running") == [0, 0, 0, 1]
    assert {line.labels["stage"] for line in result.lines} == {"dev"}


def test_the_read_asks_dagster_for_the_window_only_and_at_most_the_cap() -> None:
    client = FakeDagster([])

    _read(client, duration=timedelta(days=7), step=timedelta(minutes=30))

    assert client.reads == [((END - timedelta(days=7)).timestamp(), RUN_CAP)]


def test_a_job_without_runs_reads_zero_and_dagsters_own_jobs_are_left_out() -> None:
    client = FakeDagster([_run(END - timedelta(minutes=5), job="__ASSET_JOB")], jobs=(JOB, "idle_pipeline"))

    result = _read(client)

    assert {line.labels["job"] for line in result.lines} == {JOB, "idle_pipeline"}
    assert _values(result, job="idle_pipeline", status="succeeded") == [0, 0, 0, 0]


def test_a_job_removed_from_the_code_keeps_the_runs_it_made_in_the_window() -> None:
    result = _read(FakeDagster([_run(END - timedelta(minutes=5), job="retired_pipeline")]))

    assert _values(result, job="retired_pipeline", status="succeeded") == [0, 0, 0, 1]


def test_duration_is_the_longest_finished_run_of_the_step_and_none_without_one() -> None:
    client = FakeDagster(
        [
            _run(END - timedelta(minutes=55), seconds=60),
            _run(END - timedelta(minutes=50), "FAILURE", seconds=300),
            _run(END - timedelta(minutes=20), "STARTED", seconds=None),
            _run(END - timedelta(minutes=19), "CANCELED", started=False),
        ]
    )

    result = _read(client, PIPELINE_RUN_DURATION_SECONDS)

    assert [line.labels for line in result.lines] == [{"stage": "dev", "job": JOB}]
    assert _values(result) == [300, None, None, None]


def test_a_read_that_reaches_the_cap_is_incomplete_and_blank_before_its_oldest_run() -> None:
    # The cap is reached at a run on a step boundary: the step it ends may be missing older runs, the next may not.
    newest = [_run(END - timedelta(seconds=index + 1)) for index in range(RUN_CAP - 1)]
    oldest = _run(END - timedelta(minutes=30))
    client = FakeDagster([*newest, oldest, _run(END - timedelta(minutes=50))])

    result = _read(client)

    assert not result.complete
    assert result.covered_from == datetime.fromtimestamp(oldest.created_at, UTC)
    assert _values(result, status="succeeded") == [None, None, 0, RUN_CAP - 1]
    assert _values(result, status="failed") == [None, None, 0, 0]


def test_an_unreadable_dagster_is_unavailable_with_no_lines_and_no_service_text() -> None:
    result = _read(FakeDagster([], broken=True))

    assert (result.state, result.lines) == ("unavailable", [])
    assert "Traceback" not in result.reason


def test_a_stage_without_dagster_is_disabled() -> None:
    assert _read(None).state == "disabled"


@pytest.mark.parametrize(
    ("series", "duration", "step"),
    [
        ("trino_running_queries", HOUR, STEP),
        (PIPELINE_RUNS, timedelta(days=8), timedelta(hours=1)),
        (PIPELINE_RUNS, timedelta(0), STEP),
        (PIPELINE_RUNS, HOUR, timedelta(minutes=7)),
        (PIPELINE_RUNS, HOUR, timedelta(0)),
    ],
)
def test_a_series_or_window_outside_the_bound_is_refused(series: str, duration: timedelta, step: timedelta) -> None:
    with pytest.raises(ValueError):
        read_pipeline_series(FakeDagster([]), series, "dev", end=END, duration=duration, step=step)
