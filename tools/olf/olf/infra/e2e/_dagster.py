"""Dagster product-job launch/poll and repository-location discovery."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import requests
from openlakeforge_core import Product
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import (  # noqa: F401 - re-exported
    DagsterClient,
    DagsterHTTPError,
    DagsterTransientError,
)

from olf.common import log
from olf.infra import access, k8s
from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e._health import _bounded_pod_diagnostics
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl, load_provider_contracts_or_raise

DAGSTER_JOB_TIMEOUT_SECONDS = 1800
# No root overrides modules/orchestration/dagster's `release_name`: each stage
# runs its own "dagster" release, and its namespace is what tells them apart.
# The contract carries no service name, and reading one from Terraform output
# would tie e2e to platform state a contract-file run does not have (#278).
DAGSTER_RELEASE_NAME = "dagster"
DAGSTER_WEBSERVER_SERVICE_NAME = f"{DAGSTER_RELEASE_NAME}-dagster-webserver"


def _graphql_over(route: access.Route) -> Callable[[str, Mapping[str, Any] | None], Mapping[str, Any]]:
    """DagsterClient's GraphQL transport, sent through the route's proxy exemption and CA."""
    session = route.session()

    def request_json(query: str, variables: Mapping[str, Any] | None) -> Mapping[str, Any]:
        try:
            response = session.post(
                f"{route.url}/graphql",
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

    return request_json


def launch_and_poll_dagster_jobs(
    cfg: E2EConfig, *, products: Sequence[Product] | None = None, tags: Mapping[str, str] | None = None
) -> None:
    log.step("Launching and polling Dagster product jobs...")
    assert cfg.dagster_local_port is not None
    webserver_service_name = DAGSTER_WEBSERVER_SERVICE_NAME
    log_path = f"/tmp/openlakeforge-{cfg.env}-dagster-port-forward.log"
    with access.service_route(
        f"stage/{cfg_environ(cfg).get(CONTRACT_STAGE_ENV, 'dev')}/orchestration",
        service=webserver_service_name,
        remote_port=80,
        namespace=cfg.namespace,
        local_port=cfg.dagster_local_port,
        log_path=log_path,
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg_environ(cfg),
    ) as route:
        if not k8s.http_wait(f"{route.url}/server_info", attempts=90, delay=2, session=route.session()):
            raise E2EError("Dagster endpoint did not become reachable.")
        location_names = expected_repository_location_names(cfg)
        client = DagsterClient(
            f"{route.url}/graphql", expected_location_names=location_names, request_json=_graphql_over(route)
        )
        timeout_seconds = int(cfg_environ(cfg).get("DAGSTER_JOB_TIMEOUT_SECONDS", str(DAGSTER_JOB_TIMEOUT_SECONDS)))
        for product in products or cfg.inventory.products:
            job = product.job_name
            try:
                run_id = client.launch(job, tags=tags)
            except ServiceClientError as exc:
                diagnostics = _bounded_pod_diagnostics(
                    cfg,
                    [webserver_service_name, *expected_user_code_pods(cfg, location_names)],
                )
                raise E2EError(f"{exc}\nDagster diagnostics:\n{diagnostics}") from exc
            log.info(f"{job}: launched ({run_id})")
            try:
                client.poll(
                    job,
                    run_id,
                    timeout_seconds=timeout_seconds,
                    on_status=log.info,
                    failure_details=lambda failed_run: run_pod_failure_details(cfg, failed_run),
                )
            except ServiceClientError as exc:
                raise E2EError(str(exc)) from exc


def run_pod_failure_details(cfg: E2EConfig, run_id: str) -> str:
    # Dagster's worker health event says only "K8s job failed"; the pod's
    # termination reason distinguishes an OOM from another worker failure.
    try:
        payload = json.loads(
            kubectl(
                cfg,
                ["get", "pods", "-n", cfg.namespace, "-l", f"dagster/run-id={run_id}", "-o", "json"],
                capture=True,
            )
        )
    except (E2EError, json.JSONDecodeError):
        return ""
    lines = []
    for pod in payload.get("items", []):
        status = pod.get("status", {})
        for container in [*status.get("initContainerStatuses", []), *status.get("containerStatuses", [])]:
            for state in (container.get("state", {}), container.get("lastState", {})):
                terminated = state.get("terminated")
                if terminated and terminated.get("exitCode", 0) != 0:
                    lines.append(
                        f"\n  pod {pod['metadata']['name']}, container {container['name']}: "
                        f"{terminated.get('reason', 'terminated')} (exit code {terminated.get('exitCode')})"
                    )
    return "".join(lines)


def expected_user_code_pods(cfg: E2EConfig, location_names: Sequence[str]) -> list[str]:
    """Discover configured user-code deployments for bounded failure diagnostics."""
    try:
        raw = kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-o", "json"], capture=True)
        payload = json.loads(raw)
    except (E2EError, json.JSONDecodeError):
        return []
    from olf.infra.deployment.activation import _RELEASE as ACTIVATION_RELEASE

    # User code is the platform release's subchart under `olf infra deploy`, and its
    # own release once `olf infra project deploy` activates a revision.
    releases = {DAGSTER_RELEASE_NAME, ACTIVATION_RELEASE}
    return [
        str(item.get("metadata", {}).get("name"))
        for item in payload.get("items", [])
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "dagster-user-deployments"
        and item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/instance") in releases
        and item.get("metadata", {}).get("labels", {}).get("deployment") in location_names
    ]


def expected_repository_location_names(cfg: E2EConfig) -> list[str]:
    """This run's stage's Dagster code locations, from its v3 contract entry.

    `applied_contract_environment` has already validated the contract and
    recorded the stage it served, so this reads the same document the
    contract file or Terraform output supplied.
    """
    stage = cfg_environ(cfg).get(CONTRACT_STAGE_ENV, "")
    stages = load_provider_contracts_or_raise(cfg).get("stages") or {}
    code_locations = (stages.get(stage) or {}).get("orchestration", {}).get("code_locations")
    if not code_locations:
        raise E2EError(f"provider_contracts.stages.{stage}.orchestration.code_locations is required.")
    return [location["name"] for location in code_locations]
