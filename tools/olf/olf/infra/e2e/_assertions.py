"""Superset dashboard and OpenMetadata domain/data-product assertions."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from openlakeforge_core import LakehouseInventory, ProductSla, gold_role_tag, scan_gold_roles
from openlakeforge_services import reports
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.governance import FRESHNESS_POLICY_PROPERTY, freshness_policy, sla_payload
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError, OpenMetadataTransientError
from openlakeforge_services.superset import SupersetClient

from olf.common import config, log
from olf.common.project import ProjectSpec
from olf.infra import access, k8s, superset, trino
from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl, load_provider_contracts_or_raise
from olf.infra.e2e._trino import product_gold_tables, stage_catalog_name
from olf.infra.openmetadata._auth import admin_password, automation_token
from olf.infra.openmetadata._ingestion import JOB_NAME, run_trino_ingestion


@contextmanager
def superset_client(cfg: E2EConfig) -> Iterator[SupersetClient]:
    """A Superset client for this run's stage, reached through its service route."""
    assert cfg.superset_local_port is not None
    log_path = f"/tmp/openlakeforge-{cfg.env}-superset-port-forward.log"
    with access.service_route(
        f"stage/{config.env(CONTRACT_STAGE_ENV, 'dev', environ=cfg_environ(cfg))}/reporting",
        service="superset",
        remote_port=8088,
        namespace=cfg.namespace,
        local_port=cfg.superset_local_port,
        log_path=log_path,
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg_environ(cfg),
    ) as route:
        if not k8s.http_wait(f"{route.url}/health", attempts=90, delay=2, session=route.session()):
            raise E2EError("Superset endpoint did not become reachable.")
        password = superset.admin_password_for(cfg.namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        yield route.client(SupersetClient, password=password)


def check_superset_dashboards(cfg: E2EConfig) -> None:
    log.step("Checking Superset report imports...")
    try:
        with superset_client(cfg) as client:
            dashboards = client.dashboards()
    except ServiceClientError as exc:
        raise E2EError(str(exc)) from exc
    assert_superset_dashboards(dashboards, discovered_dashboards(cfg))


def discovered_dashboards(cfg: E2EConfig) -> dict[str, str]:
    """Read the slug/title of every dashboard actually exported.

    A product's descriptor cannot say what its dashboard is titled or slugged
    as, or how many it exports — that identity lives only in the
    source-controlled Superset export YAML under
    ``<report_source_dir>/dashboards/*.yaml`` (or ``.yml``, both of which
    ``reports.build_report_bundle`` packages), which is what actually gets
    imported. Reading it here (rather than inventing slug/title from
    id/displayName) is what lets a product export a differently named or
    multiple dashboards without failing this check.

    Every bundle deployment imports (a directory with a ``metadata.yaml``) must export one.
    """
    expected: dict[str, str] = {}
    for dashboard in sorted(cfg.inventory.dashboards, key=lambda item: item.report_source_dir):
        report_dir = cfg.repo_root / dashboard.report_source_dir
        dashboard_files = reports.discover_dashboard_files(report_dir)
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


@contextmanager
def openmetadata_client(cfg: E2EConfig) -> Iterator[tuple[OpenMetadataClient, str | None]]:
    """An OpenMetadata client for the shared platform, reached through its service route, and the automation token
    it carries (None when it must log in as the admin)."""
    assert cfg.openmetadata_local_port is not None
    log_path = f"/tmp/openlakeforge-{cfg.env}-openmetadata-port-forward.log"
    with access.service_route(
        "shared/governance_service",
        service="openmetadata",
        remote_port=8585,
        namespace=cfg.platform_namespace,
        local_port=cfg.openmetadata_local_port,
        log_path=log_path,
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg_environ(cfg),
    ) as route:
        if not k8s.http_wait(f"{route.url}/api/v1/system/config/jwks", attempts=90, delay=2, session=route.session()):
            raise E2EError("OpenMetadata endpoint did not become reachable.")
        token = automation_token(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        yield route.client(OpenMetadataClient, token=token), token


def check_openmetadata_assets(cfg: E2EConfig) -> None:
    log.step("Checking OpenMetadata domains and data products...")
    with openmetadata_client(cfg) as (client, token):
        password = (
            "" if token else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        )
        assert_openmetadata_assets(client, cfg.inventory, password)
        stages = load_provider_contracts_or_raise(cfg).get("stages")
        if not stages:
            # A v2 (pre-stage) contract: stage-qualified roots never existed.
            log.info("Skipping OpenMetadata stage roots: the provider contract has no stage index.")
            return
        stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
        database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=cfg_environ(cfg))
        assert_openmetadata_stage_roots(
            client,
            stage=stage,
            stage_contract=stages[stage],
            database_fqn=database_fqn,
        )
        gold_tables = [table for product in cfg.inventory.products for table in product_gold_tables(cfg, product)]
        assert_openmetadata_product_graph(
            client,
            cfg.inventory,
            database_fqn=database_fqn,
            tables={product.id: product_gold_tables(cfg, product) for product in cfg.inventory.products},
        )
        try:
            assert_openmetadata_stage_lineage(client, stage=stage, database_fqn=database_fqn, tables=gold_tables)
        except E2EError as exc:
            # OpenMetadata answers an OpenLineage POST with 200 whatever it made of the events, and dbt-ol's
            # own report is lost with the run pod; the server log names each dataset it could not resolve.
            raise E2EError(f"{exc}\nOpenMetadata's OpenLineage log lines:\n{openmetadata_lineage_log(cfg)}") from exc


def _project(cfg: E2EConfig) -> ProjectSpec:
    """The project this run deploys, as every other project-scoped check resolves it."""
    return ProjectSpec(root=cfg.repo_root, distribution_root=cfg.distribution_root)


def _admin_login(cfg: E2EConfig, token: str | None) -> dict[str, str]:
    """The admin credentials a token-less client logs in with, as `check_openmetadata_assets` reads them."""
    if token:
        return {}
    return {
        "admin_email": config.env("OPENMETADATA_ADMIN_EMAIL", "admin@open-metadata.org", environ=cfg_environ(cfg)),
        "admin_password": admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ),
    }


def check_openmetadata_drift(cfg: E2EConfig) -> None:
    """A clean deploy leaves nothing to report: every declared entity exists and matches Git, none is stale."""
    from olf.infra.openmetadata._drift import drift_report

    log.step("Checking OpenMetadata against Git (drift report)...")
    with openmetadata_client(cfg) as (client, token):
        try:
            items = drift_report(cfg_environ(cfg), client, project=_project(cfg), **_admin_login(cfg, token))
        except OpenMetadataError as exc:
            raise E2EError(str(exc)) from exc
    if items:
        raise E2EError("OpenMetadata differs from Git after a clean deploy:\n" + "\n".join(i.line() for i in items))


def check_openmetadata_redeploy_is_idempotent(cfg: E2EConfig) -> None:
    """Deploying the same revision again changes nothing: the deploy's reads find every owned field already in place."""
    from olf.infra.openmetadata._drift import redeploy_writes

    log.step("Redeploying OpenMetadata governance metadata, expecting no write...")
    with openmetadata_client(cfg) as (client, token):
        try:
            writes = redeploy_writes(cfg_environ(cfg), client, project=_project(cfg), **_admin_login(cfg, token))
        except OpenMetadataError as exc:
            raise E2EError(str(exc)) from exc
    if writes:
        raise E2EError("A repeat deploy of the same revision wrote to OpenMetadata:\n" + "\n".join(writes))


def crawl_workflows_without_override(cronjobs: Mapping[str, Any]) -> list[str]:
    """The names of crawl CronJobs in a `kubectl get cronjob -o json` list whose workflow lacks `overrideMetadata`."""
    names = []
    for item in cronjobs.get("items") or ():
        containers = item["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        workflow = next((e["value"] for e in containers[0]["env"] if e["name"] == "OM_WORKFLOW"), "")
        config_ = (yaml.safe_load(workflow) or {}).get("source", {}).get("sourceConfig", {}).get("config", {})
        if config_.get("overrideMetadata") is not True:
            names.append(item["metadata"]["name"])
    return names


def check_openmetadata_crawled_descriptions(cfg: E2EConfig) -> None:
    """dbt owns table and column descriptions: the crawl carries `overrideMetadata`, and each Gold table the crawl
    found has the description (and a column description) `persist_docs` wrote as Trino comments.

    A table OpenMetadata already held before its first crawl (`deploy-metadata` registers them) gets its
    descriptions from the crawl only with `overrideMetadata`, so this check runs a crawl itself.
    """
    log.step("Checking the OpenMetadata crawl refreshes descriptions...")
    namespace = trino.service_namespace(cfg_environ(cfg), cfg.namespace)
    listing = json.loads(
        kubectl(
            cfg,
            ["get", "cronjob", "-n", namespace, "-l", f"openlakeforge.io/job={JOB_NAME}", "-o", "json"],
            capture=True,
        )
    )
    if not listing.get("items"):
        raise E2EError(f"no OpenMetadata crawl CronJob in namespace {namespace}")
    without = crawl_workflows_without_override(listing)
    if without:
        raise E2EError(f"the crawl workflow of {without} does not set overrideMetadata")
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=cfg_environ(cfg))
    gold_tables = [table for product in cfg.inventory.products for table in product_gold_tables(cfg, product)]
    # `deploy-metadata` crawls once, before the pipelines build the Gold tables, and the schedule is hourly: a crawl
    # that sees them has to be run here, as `deploy-metadata` runs it.
    environ = cfg_environ(cfg)
    log.step("Crawling Trino into OpenMetadata after the pipelines...")
    run_trino_ingestion(
        environ,
        namespace=cfg.platform_namespace,
        service=config.env("OPENMETADATA_CATALOG_SERVICE", environ=environ)
        or config.env("OPENLAKEFORGE_CATALOG_PROVIDER", environ=environ),
    )
    with openmetadata_client(cfg) as (client, token):
        password = (
            "" if token else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        )
        _openmetadata_e2e_login(client, password)
        missing = []
        for table in gold_tables:
            fqn = f"{database_fqn}.{table}"
            path = f"/api/v1/tables/name/{urllib.parse.quote(fqn, safe='')}"
            entity = client.request("GET", path, params={"fields": "columns"})
            if not entity.get("description") or not any(c.get("description") for c in entity.get("columns") or ()):
                missing.append(fqn)
    if missing:
        raise E2EError(f"Gold tables without a crawled table or column description in OpenMetadata: {missing}")


# Lines the OpenMetadata 1.13 server writes for OpenLineage requests: the access log line of each POST and the
# resolver's and lineage resource's warnings (could not resolve a dataset, failed to create a table or an edge).
_LINEAGE_LOG_MARKERS = (
    "openlineage",
    "resolve input dataset",
    "resolve output dataset",
    "create table",
    "lineage edge",
)
_LINEAGE_LOG_LINES = 80


def openmetadata_lineage_log(cfg: E2EConfig) -> str:
    try:
        text = kubectl(cfg, ["logs", "-n", cfg.platform_namespace, "deploy/openmetadata"], capture=True)
    except E2EError as exc:
        return f"(cannot read the OpenMetadata log: {exc})"
    lines = [line for line in text.splitlines() if any(marker in line.lower() for marker in _LINEAGE_LOG_MARKERS)]
    return "\n".join(lines[-_LINEAGE_LOG_LINES:]) or "(none: no OpenLineage request reached OpenMetadata)"


def assert_openmetadata_stage_lineage(
    client: OpenMetadataClient, *, stage: str, database_fqn: str, tables: Iterable[str]
) -> None:
    """Every governed stage emits its own lineage (#131), and OpenMetadata
    resolves each dataset by `<service>.<database>.<schema>.<table>` (1.13+).
    So this stage's Gold tables must have upstream lineage, and none of it may
    reach another stage's database: a dataset name that lost its database
    matches by schema and table alone, which are identical in every stage."""
    log.step(f"Checking OpenMetadata lineage for stage {stage}...")
    service_prefix = database_fqn.split(".", 1)[0] + "."
    missing = []
    for table in tables:
        fqn = f"{database_fqn}.{table}"
        path = f"/api/v1/lineage/table/name/{urllib.parse.quote(fqn, safe='')}?upstreamDepth=3&downstreamDepth=0"
        try:
            lineage = client.request("GET", path)
        except OpenMetadataError as exc:
            raise E2EError(f"cannot read OpenMetadata lineage for {fqn}: {exc}") from exc
        nodes = [str(node.get("fullyQualifiedName", "")) for node in lineage.get("nodes") or ()]
        foreign = sorted(n for n in nodes if n.startswith(service_prefix) and not n.startswith(f"{database_fqn}."))
        if foreign:
            raise E2EError(f"{fqn}'s lineage reaches another stage's tables: {foreign}")
        if not lineage.get("upstreamEdges"):
            missing.append(fqn)
    if missing:
        raise E2EError(f"stage {stage}'s Gold tables have no upstream lineage in OpenMetadata: {missing}")


def _attached_asset_ids(client: OpenMetadataClient, product: str) -> set[str]:
    """Every asset id a data product holds, paged by row offset until the listing's `total` (at most 1000 a page).

    The offset counts rows, not distinct ids: a page can repeat a row when the listing shifts between requests.
    """
    held: set[str] = set()
    rows_read = 0
    while True:
        page = client.request(
            "GET", f"/api/v1/dataProducts/name/{product}/assets", params={"limit": 1000, "offset": rows_read}
        )
        data = page.get("data") or []
        rows_read += len(data)
        held |= {asset["id"] for asset in data}
        if not data or rows_read >= (page.get("paging") or {}).get("total", rows_read):
            return held


def assert_openmetadata_product_graph(
    client: OpenMetadataClient, inventory: LakehouseInventory, *, database_fqn: str, tables: Mapping[str, list[str]]
) -> None:
    """OpenMetadata holds the product graph Git declares: each `consumesFrom` edge, each Gold table with its role tag,
    and every Gold table attached to its own product. `tables` maps a product to its `<schema>.<table>` names."""
    log.step("Checking OpenMetadata product dependencies, Gold roles and attached assets...")
    roles, _ = scan_gold_roles(inventory.lakehouse_root / "gold", list(tables))
    problems: list[str] = []
    for consumer, producers in inventory.product_dependencies.producers_by_consumer.items():
        found = client.request("GET", f"/api/v1/dataProducts/name/{consumer}", params={"fields": "consumesFrom"})
        held = {ref["id"] for ref in found.get("consumesFrom") or ()}
        for producer in producers:
            producer_id = client.request("GET", f"/api/v1/dataProducts/name/{producer}").get("id")
            if producer_id not in held:
                problems.append(f"{consumer} does not consume from {producer}")
    for product, names in tables.items():
        attached = _attached_asset_ids(client, product)
        for name in names:
            fqn = f"{database_fqn}.{name}"
            table = client.request(
                "GET", f"/api/v1/tables/name/{urllib.parse.quote(fqn, safe='')}", params={"fields": "tags"}
            )
            role = roles.get(product, {}).get(name.rpartition(".")[2])
            if role and gold_role_tag(role) not in {tag["tagFQN"] for tag in table.get("tags") or ()}:
                problems.append(f"{fqn} lacks {gold_role_tag(role)}")
            if table.get("id") not in attached:
                problems.append(f"{fqn} is not attached to data product {product}")
    if problems:
        raise E2EError("OpenMetadata is missing parts of the product graph:\n" + "\n".join(problems))


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
    # Absent: the unqualified services registered before stage qualification,
    # and this stage's dashboard service when it does not run analytics (the
    # bootstrap removes it when analytics is turned off).
    absent = ["/api/v1/services/pipelineServices/name/dagster", "/api/v1/services/dashboardServices/name/superset"]
    if stage_contract.get("reporting"):
        name = stage_contract["reporting"].get("dashboard_service_name", f"superset_{stage}")
        expected[f"/api/v1/services/dashboardServices/name/{name}"] = "dashboard"
    else:
        absent.append(f"/api/v1/services/dashboardServices/name/superset_{stage}")
    for path, what in expected.items():
        try:
            client.request("GET", path)
        except OpenMetadataError as exc:
            raise E2EError(f"OpenMetadata is missing stage {stage}'s {what} root ({path}): {exc}") from exc
    for path in absent:
        try:
            client.request("GET", path, ok_statuses=(404,))
        except OpenMetadataError as exc:
            raise E2EError(f"a service that should not be registered still is ({path}): {exc}") from exc


def assert_openmetadata_assets(client: OpenMetadataClient, inventory: LakehouseInventory, password: str = "") -> None:
    _openmetadata_e2e_login(client, password)
    for domain in inventory.domain_names:
        client.request("GET", f"/api/v1/domains/name/{domain}")
    for product, candidates in inventory.openmetadata_data_products.items():
        if not _first_existing_data_product(client, candidates):
            raise E2EError(f"missing OpenMetadata data product {product}")


def sla_mismatches(product_id: str, entity: Mapping[str, Any], sla: ProductSla) -> list[str]:
    """How a data product read from OpenMetadata differs from the SLA its descriptor declares; empty when it matches."""
    problems = [
        f"{product_id}: sla.{key} is {(entity.get('sla') or {}).get(key)!r}, declared {value!r}"
        for key, value in sla_payload(sla)["sla"].items()
        if (entity.get("sla") or {}).get(key) != value
    ]
    policy = freshness_policy(sla)
    if policy and (entity.get("extension") or {}).get(FRESHNESS_POLICY_PROPERTY) != policy:
        problems.append(f"{product_id}: extension {FRESHNESS_POLICY_PROPERTY} is not the declared policy {policy}")
    return problems


def check_openmetadata_product_sla(cfg: E2EConfig) -> None:
    """Each product that declares an `sla` shows it natively in OpenMetadata, with the rest in the policy extension."""
    declared = [(product.id, product.sla) for product in cfg.inventory.products if product.sla]
    if not declared:
        log.info("No product declares an sla; nothing to check in OpenMetadata.")
        return
    log.step("Checking the SLA of each data product in OpenMetadata...")
    problems: list[str] = []
    with openmetadata_client(cfg) as (client, token):
        _openmetadata_e2e_login(
            client,
            "" if token else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ),
        )
        for product_id, sla in declared:
            entity = client.request("GET", f"/api/v1/dataProducts/name/{product_id}", params={"fields": "extension"})
            problems += sla_mismatches(product_id, entity, sla)
    if problems:
        raise E2EError("OpenMetadata does not hold the declared SLA:\n" + "\n".join(problems))
    log.info(f"{len(declared)} data product SLA(s) match governance.yml.")


def _openmetadata_e2e_login(client: OpenMetadataClient, password: str) -> None:
    if client.token:  # the automation bot's: with SSO on, basic login answers 403
        return
    last_error: Exception | None = None
    for _ in range(60):
        try:
            client.login("admin@open-metadata.org", password)
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
