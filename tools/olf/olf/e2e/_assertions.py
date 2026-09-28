"""Superset dashboard and OpenMetadata domain/data-product assertions."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from openlakeforge_domain import LakehouseInventory

from olf import config, k8s, log, superset
from olf.clients.base import ServiceClientError
from olf.clients.openmetadata import OpenMetadataClient, OpenMetadataError, OpenMetadataTransientError
from olf.clients.superset import SupersetClient
from olf.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise
from olf.e2e._trino import stage_catalog_name


def check_superset_dashboards(cfg: E2EConfig) -> None:
    log.step("Checking Superset report imports...")
    assert cfg.superset_local_port is not None
    log_path = f"/tmp/openlakeforge-{cfg.env}-superset-port-forward.log"
    with k8s.port_forward(
        "superset",
        8088,
        cfg.namespace,
        local_port=cfg.superset_local_port,
        log_path=log_path,
        kube_context=cfg.kube_context,
    ):
        base_url = f"http://127.0.0.1:{cfg.superset_local_port}"
        if not k8s.http_wait(f"{base_url}/health", attempts=90, delay=2):
            raise E2EError("Superset endpoint did not become reachable.")
        try:
            dashboards = SupersetClient(base_url).dashboards()
        except ServiceClientError as exc:
            raise E2EError(str(exc)) from exc
    assert_superset_dashboards(dashboards, discovered_dashboards(cfg))


def discovered_dashboards(cfg: E2EConfig) -> dict[str, str]:
    """Read the slug/title of every dashboard actually exported.

    A product's descriptor cannot say what its dashboard is titled or slugged
    as, or how many it exports — that identity lives only in the
    source-controlled Superset export YAML under
    ``<report_source_dir>/dashboards/*.yaml`` (or ``.yml``, both of which
    ``superset.build_report_bundle`` packages), which is what actually gets
    imported. Reading it here (rather than inventing slug/title from
    id/displayName) is what lets a product export a differently named or
    multiple dashboards without failing this check.

    Validates the descriptor registry and every bundle deployment imports.
    """
    dashboards_by_dir = {dashboard.report_source_dir: dashboard for dashboard in cfg.inventory.dashboards}
    discovered_dirs = set(superset.discover_report_dirs(cfg.repo_root))
    declared_dirs = set(dashboards_by_dir)
    if discovered_dirs != declared_dirs:
        missing = declared_dirs - discovered_dirs
        undeclared = discovered_dirs - declared_dirs
        raise E2EError(
            "Superset descriptor/filesystem mismatch: "
            f"declared but not mounted={sorted(missing)}, mounted but not declared={sorted(undeclared)}"
        )
    expected: dict[str, str] = {}
    for report_source_dir, dashboard in sorted(dashboards_by_dir.items()):
        report_dir = cfg.repo_root / report_source_dir
        dashboard_files = superset.discover_dashboard_files(report_dir)
        if not dashboard_files:
            raise E2EError(f"{report_dir / 'dashboards'}: dashboard {dashboard.name!r} exports no Superset dashboards")
        for dashboard_file in dashboard_files:
            expected.update(_read_dashboard_slug_title(dashboard_file))
    return expected


def _read_dashboard_slug_title(dashboard_file: Path) -> dict[str, str]:
    document = yaml.safe_load(dashboard_file.read_text())
    slug = document.get("slug") if isinstance(document, Mapping) else None
    title = document.get("dashboard_title") if isinstance(document, Mapping) else None
    if not slug or not title:
        raise E2EError(f"{dashboard_file}: missing slug or dashboard_title")
    return {slug: title}


def assert_superset_dashboards(dashboards: list[Mapping[str, Any]], expected: Mapping[str, str]) -> None:
    by_slug = {dashboard.get("slug"): dashboard.get("dashboard_title") for dashboard in dashboards}
    titles = {dashboard.get("dashboard_title") for dashboard in dashboards}
    missing = [
        f"{slug} ({title})" for slug, title in expected.items() if by_slug.get(slug) != title and title not in titles
    ]
    if missing:
        raise E2EError("missing Superset dashboards: " + ", ".join(missing))


def check_openmetadata_assets(cfg: E2EConfig) -> None:
    log.step("Checking OpenMetadata domains and data products...")
    assert cfg.openmetadata_local_port is not None
    log_path = f"/tmp/openlakeforge-{cfg.env}-openmetadata-port-forward.log"
    with k8s.port_forward(
        "openmetadata",
        8585,
        cfg.platform_namespace,
        local_port=cfg.openmetadata_local_port,
        log_path=log_path,
        kube_context=cfg.kube_context,
    ):
        base_url = f"http://127.0.0.1:{cfg.openmetadata_local_port}"
        if not k8s.http_wait(f"{base_url}/api/v1/system/config/jwks", attempts=90, delay=2):
            raise E2EError("OpenMetadata endpoint did not become reachable.")
        client = OpenMetadataClient(base_url)
        assert_openmetadata_assets(client, cfg.inventory)
        stages = load_provider_contracts_or_raise(cfg).get("stages")
        if not stages:
            # A v2 (pre-stage) contract: stage-qualified roots never existed.
            log.info("Skipping OpenMetadata stage roots: the provider contract has no stage index.")
            return
        stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
        assert_openmetadata_stage_roots(
            client,
            stage=stage,
            stage_contract=stages[stage],
            database_fqn=config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", ""),
        )


def assert_openmetadata_stage_roots(
    client: OpenMetadataClient, *, stage: str, stage_contract: Mapping[str, Any], database_fqn: str
) -> None:
    """The shared OpenMetadata represents this stage under its own roots (#131):
    its `<catalog service>.lakehouse_<stage>` database, its `dagster_<stage>`
    pipeline service, and its `superset_<stage>` dashboard service when the
    stage runs analytics -- and the unqualified services every deployment
    registered before stage qualification are gone."""
    log.step(f"Checking OpenMetadata service roots for stage {stage}...")
    if not database_fqn:
        raise E2EError("OPENLAKEFORGE_CATALOG_DATABASE_FQN is unset; cannot locate this stage's database root.")
    expected = {
        f"/api/v1/databases/name/{database_fqn}": f"database {database_fqn}",
        "/api/v1/services/pipelineServices/name/"
        + (stage_contract.get("orchestration") or {}).get("pipeline_service_name", f"dagster_{stage}"): "pipeline",
    }
    if stage_contract.get("reporting"):
        name = stage_contract["reporting"].get("dashboard_service_name", f"superset_{stage}")
        expected[f"/api/v1/services/dashboardServices/name/{name}"] = "dashboard"
    for path, what in expected.items():
        try:
            client.request("GET", path)
        except OpenMetadataError as exc:
            raise E2EError(f"OpenMetadata is missing stage {stage}'s {what} root ({path}): {exc}") from exc
    for path in ("/api/v1/services/pipelineServices/name/dagster", "/api/v1/services/dashboardServices/name/superset"):
        try:
            client.request("GET", path, ok_statuses=(404,))
        except OpenMetadataError as exc:
            raise E2EError(f"an unqualified pre-stage service is still registered ({path}): {exc}") from exc


def assert_openmetadata_assets(client: OpenMetadataClient, inventory: LakehouseInventory) -> None:
    _openmetadata_e2e_login(client)
    for domain in inventory.domain_names:
        client.request("GET", f"/api/v1/domains/name/{domain}")
    for product, candidates in inventory.openmetadata_data_products.items():
        if not _first_existing_data_product(client, candidates):
            raise E2EError(f"missing OpenMetadata data product {product}")


def _openmetadata_e2e_login(client: OpenMetadataClient) -> None:
    last_error: Exception | None = None
    for _ in range(60):
        try:
            client.login("admin@open-metadata.org", "admin")
            return
        except OpenMetadataError as exc:
            last_error = exc
            time.sleep(2)
    raise E2EError(f"OpenMetadata login failed: {last_error}")


def _first_existing_data_product(client: OpenMetadataClient, candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        try:
            client.request("GET", f"/api/v1/dataProducts/name/{candidate}")
        except OpenMetadataTransientError:
            raise
        except OpenMetadataError:
            continue
        return candidate
    return None
