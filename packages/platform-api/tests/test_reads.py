"""The read endpoints against loopback stand-ins for Dagster's GraphQL API and OpenMetadata's REST API."""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings, SettingsError

# Nothing listens on the discard port on loopback, so a connection is refused at once.
UNREACHABLE = "http://127.0.0.1:9"

RUN = {
    "runId": "6c1f3a5e-0000-4000-8000-000000000001",
    "jobName": "orders_job",
    "status": "SUCCESS",
    "creationTime": 1761800000.5,
    "startTime": 1761800001.0,
    "endTime": 1761800090.0,
    "tags": [{"key": "dagster/from_ui", "value": "true"}],
}


class _Server(ThreadingHTTPServer):
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeDagster(_Server):
    """Answers the Jobs, Runs and Run queries by operation name, as Dagster 1.13 shapes them."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = [RUN]
        self.fail_status: int | None = None
        self.graphql_errors: list[dict[str, Any]] = []
        self.variables: list[dict[str, Any]] = []
        server = self

        class Handler(_Handler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's handler name
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.variables.append(request["variables"])
                if server.fail_status:
                    return self.reply(server.fail_status, {"message": "down"})
                if server.graphql_errors:
                    return self.reply(200, {"data": None, "errors": server.graphql_errors})
                self.reply(200, {"data": server.answer(request["query"], request["variables"])})

        super().__init__(Handler)

    def answer(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "query Jobs" in query:
            jobs = [{"name": "orders_job", "description": "Orders"}, {"name": "__ASSET_JOB", "description": None}]
            repository = {"name": "__repository__", "location": {"name": "project-code"}, "jobs": jobs}
            return {"repositoriesOrError": {"__typename": "RepositoryConnection", "nodes": [repository]}}
        if "query Runs" in query:
            job = variables["filter"].get("pipelineName")
            runs = [run for run in self.runs if not job or run["jobName"] == job][: variables["limit"]]
            return {"runsOrError": {"__typename": "Runs", "results": runs}}
        run = next((run for run in self.runs if run["runId"] == variables["runId"]), None)
        if run is None:
            return {"runOrError": {"__typename": "RunNotFoundError", "message": "not found"}}
        return {"runOrError": {"__typename": "Run", **run}}


class FakeOpenMetadata(_Server):
    """Serves domains and data products to any bearer token, recording each Authorization header."""

    def __init__(self) -> None:
        self.authorizations: list[str | None] = []
        self.fail_status: int | None = None
        self.domains: list[dict[str, Any]] = [{"name": "sales", "displayName": "Sales"}, {"name": "finance"}]
        self.data_products: list[dict[str, Any]] = [
            {"name": "orders_mart", "displayName": "Orders", "domains": [{"name": "sales"}]}
        ]
        server = self

        class Handler(_Handler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's handler name
                server.authorizations.append(self.headers.get("Authorization"))
                if server.fail_status:
                    return self.reply(server.fail_status, {"code": server.fail_status, "message": "refused"})
                path = urllib.parse.urlsplit(self.path).path
                collection = {"/api/v1/domains": server.domains, "/api/v1/dataProducts": server.data_products}
                if path not in collection:
                    return self.reply(404, {"message": "not found"})
                self.reply(200, {"data": collection[path], "paging": {"total": len(collection[path])}})

        super().__init__(Handler)


@pytest.fixture
def dagster() -> Iterator[FakeDagster]:
    fake = FakeDagster()
    yield fake
    fake.close()


@pytest.fixture
def openmetadata() -> Iterator[FakeOpenMetadata]:
    fake = FakeOpenMetadata()
    yield fake
    fake.close()


def _settings(settings: Settings, **services: Any) -> Settings:
    raw = {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": list(settings.stages),
    }
    return Settings.from_mapping({**raw, **services})


@pytest.fixture
def api(settings: Settings, dagster: FakeDagster, openmetadata: FakeOpenMetadata) -> TestClient:
    # prod is enabled but has no Dagster: a stage the matrix allows that the deployment does not serve.
    configured = _settings(
        settings, dagster_graphql_urls={"dev": f"{dagster.url}/graphql"}, openmetadata_url=openmetadata.url
    )
    return TestClient(create_app(configured))


def _as(mint: Mint, role: str) -> dict[str, str]:
    return bearer(mint(groups=list(ROLE_MAPPING[role])))


DAGSTER_PATHS = ["/api/v1/stages/dev/jobs", "/api/v1/stages/dev/runs", f"/api/v1/stages/dev/runs/{RUN['runId']}"]
OPENMETADATA_PATHS = ["/api/v1/domains", "/api/v1/data-products"]


@pytest.mark.parametrize("path", DAGSTER_PATHS)
@pytest.mark.parametrize(("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403)])
def test_dagster_reads_admit_data_engineer_and_above(
    api: TestClient, mint: Mint, path: str, role: str, status: int
) -> None:
    assert api.get(path, headers=_as(mint, role)).status_code == status


@pytest.mark.parametrize("path", DAGSTER_PATHS)
def test_dagster_reads_refuse_a_stage_that_is_not_enabled(api: TestClient, mint: Mint, path: str) -> None:
    response = api.get(path.replace("/dev/", "/staging/"), headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert "not enabled" in response.json()["detail"]


@pytest.mark.parametrize("path", DAGSTER_PATHS)
def test_dagster_reads_refuse_a_stage_with_no_dagster(api: TestClient, mint: Mint, path: str) -> None:
    response = api.get(path.replace("/dev/", "/prod/"), headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert "no Dagster" in response.json()["detail"]


@pytest.mark.parametrize("path", DAGSTER_PATHS)
def test_dagster_reads_need_a_token(api: TestClient, path: str) -> None:
    assert api.get(path).status_code == 401


def test_jobs_are_the_projects_own(api: TestClient, mint: Mint) -> None:
    response = api.get("/api/v1/stages/dev/jobs", headers=_as(mint, "data-engineer"))

    assert response.json() == {
        "jobs": [
            {"location": "project-code", "repository": "__repository__", "name": "orders_job", "description": "Orders"}
        ]
    }


def test_runs_are_filtered_by_job_and_limited(api: TestClient, dagster: FakeDagster, mint: Mint) -> None:
    dagster.runs = [RUN, {**RUN, "runId": "other", "jobName": "returns_job"}, {**RUN, "runId": "second"}]

    response = api.get("/api/v1/stages/dev/runs?job=orders_job&limit=1", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert [run["run_id"] for run in response.json()["runs"]] == [RUN["runId"]]
    assert dagster.variables[-1] == {"filter": {"pipelineName": "orders_job"}, "limit": 1}


@pytest.mark.parametrize("limit", [0, 101])
def test_a_run_limit_outside_the_bounds_is_refused(api: TestClient, mint: Mint, limit: int) -> None:
    assert api.get(f"/api/v1/stages/dev/runs?limit={limit}", headers=_as(mint, "data-engineer")).status_code == 422


def test_a_run_reads_as_its_status_and_times(api: TestClient, mint: Mint) -> None:
    response = api.get(f"/api/v1/stages/dev/runs/{RUN['runId']}", headers=_as(mint, "data-engineer"))

    assert response.json() == {
        "run_id": RUN["runId"],
        "job_name": "orders_job",
        "status": "SUCCESS",
        "created_at": 1761800000.5,
        "started_at": 1761800001.0,
        "ended_at": 1761800090.0,
        "tags": {"dagster/from_ui": "true"},
    }


def test_an_unknown_run_is_not_found(api: TestClient, mint: Mint) -> None:
    assert api.get("/api/v1/stages/dev/runs/missing", headers=_as(mint, "data-engineer")).status_code == 404


@pytest.mark.parametrize("path", DAGSTER_PATHS)
def test_an_unreachable_dagster_is_a_bad_gateway(settings: Settings, mint: Mint, path: str) -> None:
    api = TestClient(create_app(_settings(settings, dagster_graphql_urls={"dev": f"{UNREACHABLE}/graphql"})))

    response = api.get(path, headers=_as(mint, "data-engineer"))

    assert response.status_code == 502
    assert response.json() == {"detail": "Dagster for stage 'dev' is unreachable"}


def test_a_dagster_error_is_a_bad_gateway_without_its_stack(api: TestClient, dagster: FakeDagster, mint: Mint) -> None:
    dagster.graphql_errors = [{"message": "boom", "stack": ["File secret_module.py, line 1"]}]

    response = api.get("/api/v1/stages/dev/jobs", headers=_as(mint, "data-engineer"))

    assert response.status_code == 502
    assert "secret_module" not in response.text


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
@pytest.mark.parametrize("role", ["platform-admin", "viewer"])
def test_openmetadata_reads_admit_every_recognised_role(api: TestClient, mint: Mint, path: str, role: str) -> None:
    assert api.get(path, headers=_as(mint, role)).status_code == 200


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
def test_openmetadata_reads_refuse_a_token_with_no_role(
    api: TestClient, openmetadata: FakeOpenMetadata, mint: Mint, path: str
) -> None:
    assert api.get(path, headers=bearer(mint(groups=["acme:contractors"]))).status_code == 403
    assert openmetadata.authorizations == []


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
def test_the_callers_token_reaches_openmetadata_unchanged(
    api: TestClient, openmetadata: FakeOpenMetadata, mint: Mint, path: str
) -> None:
    token = mint(groups=list(ROLE_MAPPING["viewer"]))

    api.get(path, headers=bearer(token))

    assert openmetadata.authorizations == [f"Bearer {token}"]


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
def test_a_refused_token_never_reaches_openmetadata(
    api: TestClient, openmetadata: FakeOpenMetadata, mint: Mint, path: str
) -> None:
    assert api.get(path, headers=bearer(mint(aud="superset"))).status_code == 401
    assert openmetadata.authorizations == []


def test_domains_and_data_products_read_as_openmetadata_lists_them(api: TestClient, mint: Mint) -> None:
    headers = _as(mint, "viewer")

    assert api.get("/api/v1/domains", headers=headers).json() == {
        "domains": [{"name": "sales", "display_name": "Sales"}, {"name": "finance", "display_name": ""}]
    }
    assert api.get("/api/v1/data-products", headers=headers).json() == {
        "data_products": [{"name": "orders_mart", "display_name": "Orders", "domains": ["sales"]}]
    }


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
@pytest.mark.parametrize("status", [401, 403])
def test_an_openmetadata_refusal_is_passed_through(
    api: TestClient, openmetadata: FakeOpenMetadata, mint: Mint, path: str, status: int
) -> None:
    openmetadata.fail_status = status

    assert api.get(path, headers=_as(mint, "viewer")).status_code == status


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
@pytest.mark.parametrize("status", [404, 500, 503])
def test_an_openmetadata_failure_is_a_bad_gateway(
    api: TestClient, openmetadata: FakeOpenMetadata, mint: Mint, path: str, status: int
) -> None:
    openmetadata.fail_status = status

    response = api.get(path, headers=_as(mint, "viewer"))

    assert response.status_code == 502
    assert response.json() == {"detail": "OpenMetadata answered with an error"}


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
def test_an_unreachable_openmetadata_is_a_bad_gateway_that_does_not_echo_the_token(
    settings: Settings, mint: Mint, path: str
) -> None:
    api = TestClient(create_app(_settings(settings, openmetadata_url=UNREACHABLE)))
    token = mint(groups=list(ROLE_MAPPING["viewer"]))

    response = api.get(path, headers=bearer(token))

    assert response.status_code == 502
    assert response.json() == {"detail": "OpenMetadata is unreachable"}
    assert token not in response.text


@pytest.mark.parametrize("path", OPENMETADATA_PATHS)
def test_openmetadata_reads_without_an_endpoint_are_not_configured(client: TestClient, mint: Mint, path: str) -> None:
    response = client.get(path, headers=_as(mint, "viewer"))

    assert response.status_code == 404
    assert "not configured" in response.json()["detail"]


def test_a_dagster_url_for_a_stage_that_is_not_enabled_refuses_to_load(settings: Settings) -> None:
    with pytest.raises(SettingsError, match="staging"):
        _settings(settings, dagster_graphql_urls={"staging": "http://dagster.lakehouse-staging/graphql"})
