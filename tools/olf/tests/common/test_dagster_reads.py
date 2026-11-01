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


def test_a_launch_with_its_repository_and_key_queries_no_workspace_and_tags_the_run_with_the_key() -> None:
    queries: list[str] = []
    sent: list[Mapping[str, Any] | None] = []

    def request_json(query: str, variables: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        queries.append(query)
        sent.append(variables)
        launched = {"__typename": "LaunchRunSuccess", "run": {"runId": "run-9", "status": "QUEUED"}}
        return {"data": {"launchRun": launched}}

    client = DagsterClient("http://dagster/graphql", request_json=request_json)

    run_id = client.launch(
        "sales_order_revenue_pipeline",
        tags={"openlakeforge/launched-by": "ada@example.com"},
        repository=("sales-dagster", "__repository__"),
        launch_key="req-1",
        key_tag="openlakeforge/launch-key",
    )

    assert run_id == "run-9"
    assert len(queries) == 1 and "launchRun" in queries[0]
    params = sent[0]["executionParams"]  # type: ignore[index]
    assert params["selector"]["repositoryLocationName"] == "sales-dagster"
    assert params["executionMetadata"]["tags"] == [
        {"key": "openlakeforge/launched-by", "value": "ada@example.com"},
        {"key": "openlakeforge/launch-key", "value": "req-1"},
    ]


def test_a_lost_launch_response_is_found_by_the_callers_own_key_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    from openlakeforge_services import dagster as dagster_module
    from openlakeforge_services.dagster import DagsterTransientError

    lookups: list[Mapping[str, Any] | None] = []

    def request_json(query: str, variables: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if "ExistingRun" in query:
            lookups.append(variables)
            return {"data": {"runsOrError": {"__typename": "Runs", "results": [{"runId": "run-7"}]}}}
        raise DagsterTransientError("connection reset")

    monkeypatch.setattr(dagster_module, "LAUNCH_RETRY_DELAY_SECONDS", 0)
    client = DagsterClient("http://dagster/graphql", request_json=request_json)

    run_id = client.launch("job", repository=("loc", "repo"), launch_key="req-1", key_tag="openlakeforge/launch-key")

    assert run_id == "run-7"
    assert lookups == [{"tag": "openlakeforge/launch-key", "key": "req-1"}]


def _reload_entry(image: str | None, *, load_error: str | None = None) -> dict[str, Any]:
    metadata = [{"key": "host", "value": "openlakeforge-dagster"}]
    if image is not None:
        metadata.append({"key": "image", "value": image})
    loaded: dict[str, Any] = {"__typename": "RepositoryLocation"}
    if load_error:
        loaded = {"__typename": "PythonError", "message": load_error}
    return {
        "reloadRepositoryLocation": {
            "__typename": "WorkspaceLocationEntry",
            "displayMetadata": metadata,
            "locationOrLoadError": loaded,
        }
    }


def test_reloading_a_location_returns_the_image_its_code_server_reports() -> None:
    sent: list[Mapping[str, Any] | None] = []
    client = _client(_reload_entry("repo/project-code@sha256:abc"), sent)

    assert client.reload_location_image("openlakeforge-dagster") == "repo/project-code@sha256:abc"
    assert sent == [{"repositoryLocationName": "openlakeforge-dagster"}]


def test_a_location_that_reports_no_image_reloads_to_none() -> None:
    assert _client(_reload_entry(None)).reload_location_image("openlakeforge-dagster") is None


def test_a_location_that_fails_to_load_after_the_reload_is_an_error() -> None:
    client = _client(_reload_entry("repo/project-code@sha256:abc", load_error="ModuleNotFoundError: lakehouse_code"))

    with pytest.raises(ServiceClientError, match="failed to load: ModuleNotFoundError"):
        client.reload_location_image("openlakeforge-dagster")


def test_reloading_a_location_the_webserver_does_not_know_is_an_error() -> None:
    client = _client(
        {"reloadRepositoryLocation": {"__typename": "RepositoryLocationNotFound", "message": "no such location"}}
    )

    with pytest.raises(ServiceClientError, match="could not reload code location sales-dagster: no such location"):
        client.reload_location_image("sales-dagster")
