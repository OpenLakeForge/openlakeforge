"""Live check of OpenMetadata profiling (#1465): policy-selected tables have statistics, nothing else does."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from openlakeforge_core import PROFILING_CRON
from openlakeforge_services.openmetadata import OpenMetadataClient
from openlakeforge_services.profile_statistics import read_table_statistics

from olf.common import config, log
from olf.infra import trino
from olf.infra.e2e._assertions import (
    _openmetadata_e2e_login,
    check_openmetadata_drift,
    check_openmetadata_redeploy_is_idempotent,
    openmetadata_client,
)
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl
from olf.infra.e2e._trino import product_gold_tables
from olf.infra.openmetadata._auth import admin_password
from olf.infra.openmetadata._ingestion import RESOURCES
from olf.infra.openmetadata._profiler import PROFILER_JOB_NAME, deployed_profiling_targets, run_trino_profiling
from olf.infra.openmetadata._quality_sync import schema_map

# Trino types whose min and max are real values; text columns store lengths.
_VALUE_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INT",
    "BIGINT",
    "NUMBER",
    "DECIMAL",
    "DOUBLE",
    "FLOAT",
    "DATE",
    "DATETIME",
    "TIMESTAMP",
}


def _columns(client: OpenMetadataClient, fqn: str) -> dict[str, str]:
    """`column name -> OpenMetadata data type` of a table."""
    entity = client.request(
        "GET", f"/api/v1/tables/name/{urllib.parse.quote(fqn, safe='')}", params={"fields": "columns"}
    )
    return {column["name"]: str(column.get("dataType", "")) for column in entity.get("columns") or ()}


def _sample_rows(client: OpenMetadataClient, table_id: str) -> Any:
    response = client.request("GET", f"/api/v1/tables/{table_id}/sampleData", ok_statuses=(200, 404))
    return response.get("rows") or (response.get("sampleData") or {}).get("rows")


def check_openmetadata_profiles(cfg: E2EConfig) -> None:
    """The profiler stores statistics for the opted-in Fact and Dimension tables and for nothing else.

    It runs the profile as `deploy-metadata` does, after the pipelines built the tables. Statistics are time series
    (`tableProfile`, `columnProfile`) written by the profiler pod under its own token, so they are separate from the
    entity fields the reconciler owns: the repeat-deploy write count and the drift report below cover the reconciler's
    own requests only, and the checks above them cover the profiler's output.
    """
    environ = cfg_environ(cfg)
    targets = deployed_profiling_targets(environ, cfg.inventory.lakehouse_root)
    started = datetime.now(UTC)
    catalog = environ["OPENLAKEFORGE_CATALOG_NAME"]
    if not targets:
        # A supported policy state: nothing opts in, so no profiler schedule may exist for the stage.
        namespace = trino.service_namespace(environ, cfg.platform_namespace)
        run_trino_profiling(environ, namespace=cfg.platform_namespace, service="", targets=())
        listing = json.loads(
            kubectl(
                cfg,
                ["get", "cronjob", "-n", namespace, "-l", f"openlakeforge.io/job={PROFILER_JOB_NAME}", "-o", "json"],
                capture=True,
            )
        )
        if any(catalog.replace("_", "-") in item["metadata"]["name"] for item in listing.get("items") or ()):
            raise E2EError(f"no product opts in to profiling, but a profiler CronJob exists for {catalog}")
        log.info("No product opts in to profiling: no profiler schedule, nothing to verify.")
        return
    service = config.env("OPENMETADATA_CATALOG_SERVICE", environ=environ) or config.env(
        "OPENLAKEFORGE_CATALOG_PROVIDER", environ=environ
    )
    log.step("Profiling the opted-in Gold tables...")
    if not run_trino_profiling(environ, namespace=cfg.platform_namespace, service=service, targets=targets):
        raise E2EError("the OpenMetadata profiler Job did not complete (see the warning above)")
    namespace = trino.service_namespace(environ, cfg.platform_namespace)
    _assert_profiler_cronjob(cfg, namespace, catalog)

    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=environ)
    gold_schemas = schema_map("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", environ) or {}

    def table_fqn(product: str, logical: str) -> str:
        # The contract's schema FQN is the physical one (Glue prefixes it with the catalog name).
        schema_fqn = gold_schemas.get(product)
        return f"{schema_fqn}.{logical.split('.', 1)[1]}" if schema_fqn else f"{database_fqn}.{logical}"

    expected = {table_fqn(t.product, f"{t.schema}.{table}"): t for t in targets for table in t.tables}
    others = [
        fqn
        for product in cfg.inventory.products
        for table in product_gold_tables(cfg, product)
        if (fqn := table_fqn(product.id, table)) not in expected
    ]
    with openmetadata_client(cfg) as (client, token):
        password = (
            "" if token else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        )
        _openmetadata_e2e_login(client, password)
        problems: list[str] = []
        for fqn, target in sorted(expected.items()):
            types = _columns(client, fqn)
            stats = read_table_statistics(client, fqn, list(types))
            if stats.row_count is None or stats.stale:
                problems.append(f"{fqn}: no current table profile (rowCount={stats.row_count}, stale={stats.stale})")
                continue
            for column in stats.columns:
                if column.null_count is None or column.distinct_count is None:
                    problems.append(f"{fqn}.{column.name}: missing nullCount or distinctCount")
            if "min" in target.metrics:
                for column in stats.columns:
                    # A column with no non-null value has no extremum to store.
                    if (
                        types[column.name].upper() in _VALUE_TYPES
                        and (column.values_count or 0) > 0
                        and (column.minimum is None or column.maximum is None)
                    ):
                        problems.append(f"{fqn}.{column.name}: min and max were not stored")
            if "min" not in target.metrics and any(
                c.minimum is not None or c.maximum is not None or c.mean is not None for c in stats.columns
            ):
                problems.append(f"{fqn}: stored values the classification does not allow")
            table_id = client.request("GET", f"/api/v1/tables/name/{urllib.parse.quote(fqn, safe='')}")["id"]
            if _sample_rows(client, table_id):
                problems.append(f"{fqn}: sample data is stored")
        for fqn in others:
            # A product that opted out keeps the statistics it had (turning profiling off deletes only the
            # schedule), so what must be absent is a profile written by this run.
            profiled_at = read_table_statistics(client, fqn, ()).profiled_at
            if profiled_at is not None and profiled_at >= started:
                problems.append(f"{fqn}: profiled in this run, but no policy selects it")
    if problems:
        raise E2EError("OpenMetadata profiles differ from the policy:\n" + "\n".join(problems))
    # The profile has run: the reconciler still writes nothing and finds no drift.
    check_openmetadata_redeploy_is_idempotent(cfg)
    check_openmetadata_drift(cfg)


def _assert_profiler_cronjob(cfg: E2EConfig, namespace: str, catalog: str) -> None:
    name = f"{PROFILER_JOB_NAME}-{catalog.replace('_', '-')}"
    cron = json.loads(kubectl(cfg, ["get", "cronjob", name, "-n", namespace, "-o", "json"], capture=True))
    if cron["spec"]["schedule"] != PROFILING_CRON:
        raise E2EError(f"{name} runs on {cron['spec']['schedule']!r}, not {PROFILING_CRON!r}")
    resources = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["resources"]
    if resources != RESOURCES:
        raise E2EError(f"{name} requests {resources}, more or less than the crawl's {RESOURCES}")
