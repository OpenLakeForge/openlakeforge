"""The quality writer's least privilege on the running OpenMetadata (#346, ADR 0007 D13).

The stage's `olf-quality-writer-<stage>` token, read from the Secret the reconciler mounts, must publish a result
through every route the publisher uses, and be refused (403) a table description edit and a lineage edge. The case it
creates uses a check id no engine emits and is removed afterwards.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from openlakeforge_core.quality import PipelineOutcome, QualityOutcome, QualityResult
from openlakeforge_core.quality_sync import OmStatus, case_name, definition_name, epoch_millis
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError
from openlakeforge_services.quality_publisher import QualityCatalog, result_body

from olf.common import config, log
from olf.infra import k8s
from olf.infra.e2e._assertions import _openmetadata_e2e_login, openmetadata_client
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ
from olf.infra.e2e._trino import product_gold_tables, stage_catalog_name
from olf.infra.openmetadata._auth import admin_password
from olf.infra.openmetadata._quality_sync import schema_map
from olf.infra.openmetadata._quality_writer import JWT_KEY, writer_bot, writer_credentials

# Not `e2e_*`: the publication check counts the `olf.floe.e2e_*` cases on the same table.
CHECK = "olf_e2e_writer_rule"


def check_quality_writer_permissions(cfg: E2EConfig) -> None:
    log.step("Checking the quality writer's OpenMetadata permissions...")
    stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
    silver, gold = _tables(cfg)
    token = k8s.secret_value(
        writer_credentials(stage), JWT_KEY, cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ
    )
    if not token:
        raise E2EError(f"Secret {writer_credentials(stage)} holds no {JWT_KEY}")
    with openmetadata_client(cfg) as (admin, admin_token):
        password = (
            ""
            if admin_token
            else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        )
        _openmetadata_e2e_login(admin, password)
        writer = dataclasses.replace(admin, token=token)
        me = writer.request("GET", "/api/v1/users/loggedInUser")
        if me.get("name") != writer_bot(stage) or me.get("isAdmin"):
            raise E2EError(f"the stage's writer token is not the non-admin {writer_bot(stage)}: {me.get('name')}")
        case = ""
        try:
            case = _publish(writer, stage, silver)
            _expect_refused(writer, admin, silver, gold)
        finally:
            if case:
                admin.request(
                    "DELETE",
                    "/api/v1/dataQuality/testCases/name/" + urllib.parse.quote(case, safe=""),
                    params={"hardDelete": "true", "recursive": "true"},
                    ok_statuses=(200, 404),
                )


def _tables(cfg: E2EConfig) -> tuple[str, str]:
    """A registered Silver and Gold table of this stage, named as the publisher names them."""
    environ = cfg_environ(cfg)
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=environ)
    if not database_fqn:
        raise E2EError("OPENLAKEFORGE_CATALOG_DATABASE_FQN is unset; cannot locate this stage's database root.")
    product = next(iter(cfg.inventory.products), None)
    if product is None:
        raise E2EError("the inventory has no product to publish quality results for")
    domain = cfg.inventory.domain_for_product(product)
    entities = cfg.inventory.floe_entities(domain)
    if not entities:
        raise E2EError(f"domain {domain.name} has no Floe entity")
    silver_schema = (schema_map("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON", environ) or {}).get(
        domain.name
    ) or f"{database_fqn}.{domain.silver_namespace}"
    gold_schema = (schema_map("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", environ) or {}).get(
        product.id
    ) or f"{database_fqn}.{product.gold_namespace}"
    gold_table = product_gold_tables(cfg, product)[0].rsplit(".", 1)[1]
    return f"{silver_schema}.{entities[0].name}", f"{gold_schema}.{gold_table}"


def _publish(writer: OpenMetadataClient, stage: str, table_fqn: str) -> str:
    """Every publisher route with the writer's token: table lookup, definition, case, result read and write."""
    catalog = QualityCatalog(writer)
    now = datetime.now(UTC).replace(microsecond=0)
    result = QualityResult(
        engine="floe",
        stage=stage,
        domain="e2e",
        product="e2e",
        table=table_fqn.rsplit(".", 1)[1],
        check_id=CHECK,
        engine_severity="warn",
        outcome=QualityOutcome.PASSED,
        pipeline_outcome=PipelineOutcome.CONTINUED,
        observed_at=now,
        dagster_run_id=f"olf-e2e-writer-{int(now.timestamp())}",
    )
    try:
        if catalog.table_columns(table_fqn) is None:
            raise E2EError(f"the quality writer cannot find the registered table {table_fqn}")
        catalog.ensure_definition(definition_name(result), column=False)
        case = catalog.upsert_case(
            name=case_name(result),
            definition=definition_name(result),
            table_fqn=table_fqn,
            column=None,
            description="e2e: the quality writer's permissions; removed by the check.",
        )
        timestamp = epoch_millis(now)
        catalog.add_result(case, result_body(result, OmStatus.SUCCESS, timestamp))
        if not catalog.has_result(case, timestamp, result.identity):
            raise E2EError(f"the quality writer's result is not in {case}'s history")
    except OpenMetadataError as exc:
        raise E2EError(f"the quality writer could not publish a result to {table_fqn}: {exc}") from exc
    return case


def _expect_refused(writer: OpenMetadataClient, admin: OpenMetadataClient, silver: str, gold: str) -> None:
    """A description edit and a lineage edge answer 403. Each request would be harmless if it were allowed: the
    description is set to its current value, and an allowed edge is removed again before the check fails."""
    tables: dict[str, Any] = {
        fqn: writer.request("GET", "/api/v1/tables/name/" + urllib.parse.quote(fqn, safe="")) for fqn in (silver, gold)
    }
    try:
        writer.request(
            "PATCH",
            f"/api/v1/tables/{tables[silver]['id']}",
            json_body=[{"op": "add", "path": "/description", "value": tables[silver].get("description") or ""}],
            content_type="application/json-patch+json",
            ok_statuses=(403,),
        )
    except OpenMetadataError as exc:
        raise E2EError(f"the quality writer was not refused a description edit on {silver}: {exc}") from exc
    # Gold to Silver, against the data flow, so no real edge can be the one tested.
    source, target = tables[gold]["id"], tables[silver]["id"]
    edge = {"fromEntity": {"id": source, "type": "table"}, "toEntity": {"id": target, "type": "table"}}
    try:
        writer.request("PUT", "/api/v1/lineage", json_body={"edge": edge}, ok_statuses=(403,))
    except OpenMetadataError as exc:
        if exc.status == 200:
            admin.request("DELETE", f"/api/v1/lineage/table/{source}/table/{target}", ok_statuses=(200, 404))
        raise E2EError(f"the quality writer was not refused a lineage edge from {gold} to {silver}: {exc}") from exc
