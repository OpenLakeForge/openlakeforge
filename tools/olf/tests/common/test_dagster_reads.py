from collections.abc import Mapping
from typing import Any

import pytest
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterJob, DagsterRun


def _client(response: Mapping[str, Any], sent: list[Mapping[str, Any] | None] | None = None) -> DagsterClient:
    def request_json(_query: str, variables: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if sent is not None:
            sent.append(variables)
        return {"data": response}

    return DagsterClient("http://dagster/graphql", request_json=request_json)


_RUN = {
    "runId": "run-1",
    "jobName": "sales_order_revenue_pipeline",
    "status": "STARTED",
    "creationTime": 1700000000.5,
    "startTime": 1700000001.0,
    "endTime": None,
    "tags": [{"key": "dagster/schedule_name", "value": "daily"}],
}


def test_list_jobs_names_each_jobs_location_and_leaves_out_dagsters_own() -> None:
    client = _client(
        {
            "repositoriesOrError": {
                "__typename": "RepositoryConnection",
                "nodes": [
                    {
                        "name": "__repository__",
                        "location": {"name": "sales-dagster"},
                        "jobs": [
                            {"name": "__ASSET_JOB", "description": None},
                            {"name": "sales_order_revenue_pipeline", "description": "Order revenue."},
                        ],
                    }
                ],
            }
        }
    )

    assert client.list_jobs() == [
        DagsterJob("sales-dagster", "__repository__", "sales_order_revenue_pipeline", "Order revenue.")
    ]


def test_list_runs_filters_by_job_and_reads_each_run() -> None:
    sent: list[Mapping[str, Any] | None] = []
    client = _client({"runsOrError": {"__typename": "Runs", "results": [_RUN]}}, sent)

    runs = client.list_runs(job_name="sales_order_revenue_pipeline", limit=5)

    assert sent == [{"filter": {"pipelineName": "sales_order_revenue_pipeline"}, "limit": 5}]
    assert runs == [
        DagsterRun(
            run_id="run-1",
            job_name="sales_order_revenue_pipeline",
            status="STARTED",
            created_at=1700000000.5,
            started_at=1700000001.0,
            ended_at=None,
            tags={"dagster/schedule_name": "daily"},
        )
    ]


def test_list_runs_without_a_job_sends_no_filter() -> None:
    sent: list[Mapping[str, Any] | None] = []
    _client({"runsOrError": {"__typename": "Runs", "results": []}}, sent).list_runs()

    assert sent == [{"filter": {}, "limit": 20}]


def test_list_runs_reports_dagsters_own_error() -> None:
    client = _client({"runsOrError": {"__typename": "PythonError", "message": "storage unavailable"}})

    with pytest.raises(ServiceClientError, match="storage unavailable"):
        client.list_runs()


def test_get_run_returns_none_for_an_unknown_run() -> None:
    client = _client({"runOrError": {"__typename": "RunNotFoundError", "message": "Run x not found"}})

    assert client.get_run("x") is None


def test_get_run_reads_the_run() -> None:
    run = _client({"runOrError": {"__typename": "Run", **_RUN}}).get_run("run-1")

    assert run is not None and run.status == "STARTED" and run.ended_at is None
