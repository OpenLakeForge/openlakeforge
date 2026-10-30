"""Dagster GraphQL client: list jobs and runs, launch, poll and inspect runs."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import requests

from .base import ServiceClientError, TransientServiceError

DAGSTER_JOB_TIMEOUT_SECONDS = 1800
LAUNCH_RETRY_ATTEMPTS = 4
LAUNCH_RETRY_DELAY_SECONDS = 3


class DagsterTransientError(TransientServiceError):
    """Transient Dagster error (5xx, connection, timeout)."""

    pass


class DagsterHTTPError(ServiceClientError):
    """Non-transient Dagster HTTP response (normally a client error)."""

    pass


# The fields every run read selects, so `DagsterRun.from_graphql` sees one shape.
_RUN_FIELDS = "runId jobName status creationTime startTime endTime tags { key value }"


@dataclass(frozen=True)
class DagsterJob:
    location: str
    repository: str
    name: str
    description: str


@dataclass(frozen=True)
class DagsterRun:
    run_id: str
    job_name: str
    status: str
    # Unix seconds, as Dagster reports them; a queued run has no start yet.
    created_at: float
    started_at: float | None
    ended_at: float | None
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_graphql(cls, run: Mapping[str, Any]) -> DagsterRun:
        return cls(
            run_id=str(run["runId"]),
            job_name=str(run["jobName"]),
            status=str(run["status"]),
            created_at=float(run["creationTime"]),
            started_at=run.get("startTime"),
            ended_at=run.get("endTime"),
            tags={tag["key"]: tag["value"] for tag in run.get("tags") or []},
        )


class DagsterClient:
    def __init__(
        self,
        graphql_url: str,
        *,
        expected_location_names: Sequence[str] = (),
        request_json: Callable[[str, Mapping[str, Any] | None], Mapping[str, Any]] | None = None,
    ) -> None:
        self.graphql_url = graphql_url
        self.expected_location_names = tuple(expected_location_names)
        self._request_json = request_json or self._requests_graphql

    def launch(self, job_name: str, *, tags: Mapping[str, str] | None = None) -> str:
        location_name, repository_name = self.wait_for_repository(job_name)
        launch_key = f"openlakeforge-e2e-{job_name}-{uuid.uuid4().hex}"
        run_tags = {**(tags or {}), "openlakeforge/e2e-key": launch_key}
        variables = {
            "executionParams": {
                "selector": {
                    "repositoryLocationName": location_name,
                    "repositoryName": repository_name,
                    "pipelineName": job_name,
                },
                "runConfigData": {},
                "mode": "default",
                "executionMetadata": {
                    "tags": [{"key": key, "value": value} for key, value in run_tags.items()],
                },
            }
        }
        last_error: DagsterTransientError | None = None
        for attempt in range(LAUNCH_RETRY_ATTEMPTS):
            try:
                result = self.graphql(
                    """
            mutation LaunchRun($executionParams: ExecutionParams!) {
              launchRun(executionParams: $executionParams) {
                __typename
                ... on LaunchRunSuccess { run { runId status } }
                ... on RunConfigValidationInvalid { errors { message } }
                ... on PythonError { message stack }
              }
            }
            """,
                    variables,
                )["launchRun"]
                break
            except DagsterTransientError as exc:
                last_error = exc
                existing = self.find_run_by_tag(launch_key)
                if existing:
                    return existing
                if attempt + 1 == LAUNCH_RETRY_ATTEMPTS:
                    raise ServiceClientError(
                        f"Dagster launch for {job_name} failed after {LAUNCH_RETRY_ATTEMPTS} attempts: {exc}"
                    ) from exc
                time.sleep(LAUNCH_RETRY_DELAY_SECONDS)
        else:  # pragma: no cover - loop always breaks or raises
            raise ServiceClientError(f"Dagster launch failed: {last_error}")
        if result["__typename"] != "LaunchRunSuccess":
            raise ServiceClientError(f"failed to launch {job_name}: {json.dumps(result, indent=2)}")
        return result["run"]["runId"]

    def find_run_by_tag(self, launch_key: str) -> str | None:
        """Find a run created before an ambiguous HTTP response."""
        try:
            result = self.graphql(
                """
                query ExistingRun($key: String!) {
                  runsOrError(filter: {tags: [{key: "openlakeforge/e2e-key", value: $key}]}, limit: 1) {
                    __typename
                    ... on Runs { results { runId } }
                  }
                }
                """,
                {"key": launch_key},
            )["runsOrError"]
            runs = result.get("results", [])
            return str(runs[0]["runId"]) if runs else None
        except ServiceClientError:
            return None

    def list_jobs(self) -> list[DagsterJob]:
        """Every job the loaded code locations define, without Dagster's implicit asset jobs."""
        result = self.graphql(
            """
            query Jobs {
              repositoriesOrError {
                __typename
                ... on RepositoryConnection {
                  nodes { name location { name } jobs { name description } }
                }
                ... on PythonError { message }
              }
            }
            """,
        )["repositoriesOrError"]
        if result.get("__typename") != "RepositoryConnection":
            raise ServiceClientError(f"Dagster could not list its repositories: {result.get('message', result)}")
        return [
            DagsterJob(
                location=repository["location"]["name"],
                repository=repository["name"],
                name=job["name"],
                description=job.get("description") or "",
            )
            for repository in result.get("nodes", [])
            for job in repository.get("jobs", [])
            # `__ASSET_JOB` and its partitioned variants are Dagster's own, not the project's.
            if not job["name"].startswith("__")
        ]

    def list_runs(self, *, job_name: str = "", limit: int = 20) -> list[DagsterRun]:
        """The most recent runs, newest first, optionally of one job."""
        result = self.graphql(
            f"""
            query Runs($filter: RunsFilter, $limit: Int) {{
              runsOrError(filter: $filter, limit: $limit) {{
                __typename
                ... on Runs {{ results {{ {_RUN_FIELDS} }} }}
                ... on InvalidPipelineRunsFilterError {{ message }}
                ... on PythonError {{ message }}
              }}
            }}
            """,
            # Dagster's run filter still names the job `pipelineName`.
            {"filter": {"pipelineName": job_name} if job_name else {}, "limit": limit},
        )["runsOrError"]
        if result.get("__typename") != "Runs":
            raise ServiceClientError(f"Dagster could not list runs: {result.get('message', result)}")
        return [DagsterRun.from_graphql(run) for run in result.get("results", [])]

    def get_run(self, run_id: str) -> DagsterRun | None:
        """One run, or None if Dagster has no run with that id."""
        result = self.graphql(
            f"""
            query Run($runId: ID!) {{
              runOrError(runId: $runId) {{
                __typename
                ... on Run {{ {_RUN_FIELDS} }}
                ... on RunNotFoundError {{ message }}
                ... on PythonError {{ message }}
              }}
            }}
            """,
            {"runId": run_id},
        )["runOrError"]
        if result.get("__typename") == "RunNotFoundError":
            return None
        if result.get("__typename") != "Run":
            raise ServiceClientError(f"Dagster could not read run {run_id}: {result.get('message', result)}")
        return DagsterRun.from_graphql(result)

    def wait_for_repository(
        self,
        job_name: str,
        *,
        timeout_seconds: int = 90,
        delay: float = 2.0,
    ) -> tuple[str, str]:
        deadline = time.monotonic() + timeout_seconds
        last_error: ServiceClientError | None = None
        while time.monotonic() < deadline:
            try:
                return self.discover_repository(job_name)
            except ServiceClientError as exc:
                last_error = exc
                self.try_reload_repository_locations()
                time.sleep(delay)
        detail = f": {last_error}" if last_error else ""
        raise ServiceClientError(
            f"Dagster repository for {job_name} did not become ready "
            f"within {timeout_seconds} seconds{detail}. Required code location may be unavailable; "
            "inspect Dagster webserver and user-code logs."
        )

    def try_reload_repository_location(self, location_name: str) -> None:
        try:
            self.graphql(
                """
                mutation ReloadRepositoryLocation($repositoryLocationName: String!) {
                  reloadRepositoryLocation(repositoryLocationName: $repositoryLocationName) {
                    __typename
                    ... on WorkspaceLocationEntry { name }
                    ... on ReloadNotSupported { message }
                    ... on RepositoryLocationNotFound { message }
                    ... on PythonError { message }
                  }
                }
                """,
                {"repositoryLocationName": location_name},
            )
        except ServiceClientError:
            return

    def try_reload_repository_locations(self) -> None:
        for location_name in self.expected_location_names:
            self.try_reload_repository_location(location_name)

    def poll(
        self,
        job_name: str,
        run_id: str,
        *,
        timeout_seconds: int = DAGSTER_JOB_TIMEOUT_SECONDS,
        delay: float = 10.0,
        attempts: int | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        terminal = {"SUCCESS", "FAILURE", "CANCELED"}
        last_error: Exception | None = None
        last_status: str | None = None
        remaining_attempts = attempts
        deadline = time.monotonic() + timeout_seconds
        while remaining_attempts is None or remaining_attempts > 0:
            if remaining_attempts is None and time.monotonic() >= deadline:
                break
            if remaining_attempts is not None:
                remaining_attempts -= 1
            try:
                result = self.graphql(
                    """
                    query Run($runId: ID!) {
                      runOrError(runId: $runId) {
                        __typename
                        ... on Run { status }
                        ... on PythonError { message }
                      }
                    }
                    """,
                    {"runId": run_id},
                )["runOrError"]
            except DagsterTransientError as exc:
                last_error = exc
                time.sleep(delay)
                continue
            if result["__typename"] != "Run":
                raise ServiceClientError(f"could not read run {run_id}: {result}")
            status = result["status"]
            if status != last_status:
                if on_status is not None:
                    on_status(f"{job_name}: {status} ({run_id})")
                last_status = status
            if status in terminal:
                if status != "SUCCESS":
                    failures = self._step_failures(run_id)
                    raise ServiceClientError(f"{job_name} run {run_id} ended with {status}{failures}")
                return
            time.sleep(delay)
        detail = f": {last_error}" if last_error else ""
        raise ServiceClientError(f"{job_name} run {run_id} did not finish within {timeout_seconds} seconds{detail}")

    def _step_failures(self, run_id: str) -> str:
        """The failed steps' own errors, which otherwise live only in the event
        log of a cluster CI has already torn down. Best effort: a failed lookup
        must not replace the run failure being reported."""
        try:
            events = self.graphql(
                """
                query RunFailures($runId: ID!) {
                  logsForRun(runId: $runId) {
                    ... on EventConnection {
                      events {
                        ... on ExecutionStepFailureEvent {
                          stepKey
                          error { message cause { message cause { message cause { message } } } }
                        }
                      }
                    }
                  }
                }
                """,
                {"runId": run_id},
            )["logsForRun"]["events"]
        except Exception:  # noqa: BLE001
            return ""
        lines = []
        for event in events:
            if not event.get("stepKey"):
                continue
            # Dagster wraps a step's exception in DagsterExecutionStepExecutionError,
            # whose own message names only the op; the reason is down the cause chain.
            messages, error = [], event.get("error")
            while error:
                messages.append(error["message"].strip())
                error = error.get("cause")
            lines.append(f"\n  {event['stepKey']}: " + "\n    caused by: ".join(messages))
        return "".join(lines)

    def discover_repository(self, job_name: str) -> tuple[str, str]:
        workspace = self.graphql(
            """
            query Workspace {
              workspaceOrError {
                __typename
                ... on Workspace {
                  locationEntries {
                    name
                    locationOrLoadError {
                      __typename
                      ... on RepositoryLocation {
                        repositories {
                          name
                          pipelines { name }
                          jobs { name }
                        }
                      }
                      ... on PythonError { message }
                    }
                  }
                }
              }
            }
            """,
        )["workspaceOrError"]
        if workspace.get("__typename") != "Workspace":
            raise ServiceClientError(f"Dagster workspace query failed: {workspace}")

        load_errors: list[str] = []
        for entry in workspace.get("locationEntries", []):
            location = entry.get("locationOrLoadError") or {}
            if location.get("__typename") != "RepositoryLocation":
                if location.get("__typename") == "PythonError":
                    message = location.get("message", "unknown Dagster workspace error").strip()
                    load_errors.append(f"{entry.get('name')}: {message}")
                continue
            for repo in location.get("repositories", []):
                job_names = {job["name"] for job in repo.get("jobs", [])}
                pipeline_names = {pipeline["name"] for pipeline in repo.get("pipelines", [])}
                if job_name in job_names or job_name in pipeline_names:
                    return entry["name"], repo["name"]
        detail = f" Workspace load errors: {'; '.join(load_errors)}" if load_errors else ""
        raise ServiceClientError(f"Dagster job {job_name} is not available yet.{detail}")

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        data = self._request_json(query, variables)
        if data.get("errors"):
            raise ServiceClientError(json.dumps(data["errors"], indent=2))
        return data["data"]

    def _requests_graphql(self, query: str, variables: Mapping[str, Any] | None) -> Mapping[str, Any]:
        try:
            response = requests.post(
                self.graphql_url,
                json={"query": query, "variables": variables or {}},
                headers={"Accept": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 500 <= status < 600:
                raise DagsterTransientError(f"Dagster GraphQL HTTP {status}: {exc}") from exc
            raise DagsterHTTPError(f"Dagster GraphQL HTTP {status}: {exc}") from exc
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise DagsterTransientError(f"Dagster GraphQL request failed: {exc}") from exc
        return response.json()
