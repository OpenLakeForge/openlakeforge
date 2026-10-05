"""Dagster product-job launch/poll and repository-location discovery."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from openlakeforge_domain import Product

from olf import access, k8s, log
from olf.clients.base import ServiceClientError
from olf.clients.dagster import DagsterClient, DagsterHTTPError, DagsterTransientError  # noqa: F401 - re-exported
from olf.contracts import CONTRACT_STAGE_ENV
from olf.e2e._health import _bounded_pod_diagnostics
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

DAGSTER_JOB_TIMEOUT_SECONDS = 1800
# No root overrides modules/orchestration/dagster's `release_name`: each stage
# runs its own "dagster" release, and its namespace is what tells them apart.
# The contract carries no service name, and reading one from Terraform output
# would tie e2e to platform state a contract-file run does not have (#278).
DAGSTER_RELEASE_NAME = "dagster"
DAGSTER_WEBSERVER_SERVICE_NAME = f"{DAGSTER_RELEASE_NAME}-dagster-webserver"


def launch_and_poll_dagster_jobs(cfg: E2EConfig, *, products: Sequence[Product] | None = None) -> None:
    log.step("Launching and polling Dagster product jobs...")
    assert cfg.dagster_local_port is not None
    webserver_service_name = DAGSTER_WEBSERVER_SERVICE_NAME
    log_path = f"/tmp/openlakeforge-{cfg.env}-dagster-port-forward.log"
    with access.service_url(
        f"stage/{os.environ.get(CONTRACT_STAGE_ENV, 'dev')}/orchestration",
        service=webserver_service_name,
        remote_port=80,
        namespace=cfg.namespace,
        local_port=cfg.dagster_local_port,
        log_path=log_path,
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
    ) as base_url:
        if not k8s.http_wait(f"{base_url}/server_info", attempts=90, delay=2):
            raise E2EError("Dagster endpoint did not become reachable.")
        location_names = expected_repository_location_names(cfg)
        client = DagsterClient(f"{base_url}/graphql", expected_location_names=location_names)
        timeout_seconds = int(os.environ.get("DAGSTER_JOB_TIMEOUT_SECONDS", str(DAGSTER_JOB_TIMEOUT_SECONDS)))
        for product in products or cfg.inventory.products:
            job = product.job_name
            try:
                run_id = client.launch(job)
            except ServiceClientError as exc:
                diagnostics = _bounded_pod_diagnostics(
                    cfg,
                    [webserver_service_name, *expected_user_code_pods(cfg, location_names)],
                )
                raise E2EError(f"{exc}\nDagster diagnostics:\n{diagnostics}") from exc
            log.info(f"{job}: launched ({run_id})")
            try:
                client.poll(job, run_id, timeout_seconds=timeout_seconds)
            except ServiceClientError as exc:
                raise E2EError(str(exc)) from exc


def expected_user_code_pods(cfg: E2EConfig, location_names: Sequence[str]) -> list[str]:
    """Discover configured user-code deployments for bounded failure diagnostics."""
    try:
        raw = kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-o", "json"], capture=True)
        payload = json.loads(raw)
    except (E2EError, json.JSONDecodeError):
        return []
    from olf.deployment.activation import _RELEASE as ACTIVATION_RELEASE

    # User code is the platform release's subchart under `olf deploy`, and its
    # own release once `olf project deploy` activates a revision.
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
    stage = os.environ.get(CONTRACT_STAGE_ENV, "")
    stages = load_provider_contracts_or_raise(cfg).get("stages") or {}
    code_locations = (stages.get(stage) or {}).get("orchestration", {}).get("code_locations")
    if not code_locations:
        raise E2EError(f"provider_contracts.stages.{stage}.orchestration.code_locations is required.")
    return [location["name"] for location in code_locations]
