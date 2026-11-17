"""OpenMetadata quality publication assertions (#797): real OpenMetadata, real ops bucket, synthetic manifests.

The manifests are written here instead of waiting for a pipeline to warn or fail, because the demo data passes.
They use check ids no engine emits and run ids no Dagster run has, so no real result or alert can collide with
them, and everything they create is removed afterwards.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult
from openlakeforge_core.quality_sync import SyncSummary
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError
from openlakeforge_services.quality_publisher import QualityCatalog, QualityPublisher

from olf.common import config, log
from olf.infra.e2e._artifacts import ops_bucket_client
from olf.infra.e2e._assertions import openmetadata_client
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, load_provider_contracts_or_raise
from olf.infra.e2e._trino import product_gold_tables, stage_catalog_name

WARN_RULE = "e2e_warn_rule"
ERROR_RULE = "e2e_error_rule"
DBT_RULE = "e2e_dbt_rule"
RESULTS = "/api/v1/dataQuality/testCases/testCaseResults/"


def check_quality_publication(cfg: E2EConfig) -> None:
    """Published results land on existing tables of this stage only, keep WARN and ERROR apart, add history on a
    second run, and are idempotent on replay; an unregistered table stays pending and visible."""
    log.step("Checking quality publication to OpenMetadata...")
    stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=cfg_environ(cfg))
    if not database_fqn:
        raise E2EError("OPENLAKEFORGE_CATALOG_DATABASE_FQN is unset; cannot locate this stage's database root.")
    product = next((p for p in cfg.inventory.products), None)
    if product is None:
        raise E2EError("the inventory has no product to publish quality results for")
    domain = cfg.inventory.domain_for_product(product)
    entities = cfg.inventory.floe_entities(domain)
    if not entities:
        raise E2EError(f"domain {domain.name} has no Floe entity")
    silver = f"{database_fqn}.{domain.silver_namespace}.{entities[0].name}"
    gold_table = product_gold_tables(cfg, product)[0]
    gold = f"{database_fqn}.{gold_table}"

    stamp = int(datetime.now(UTC).timestamp())
    runs = [f"olf-e2e-quality-{stamp}-{i}" for i in (1, 2, 3)]
    start = datetime.now(UTC).replace(microsecond=0)
    cases = {
        f"{silver}.olf.floe.{WARN_RULE}": "Aborted",
        f"{silver}.olf.floe.{ERROR_RULE}": "Failed",
        f"{gold}.olf.dbt.{DBT_RULE}": "Success",
    }

    with ops_bucket_client(cfg) as (s3, bucket), openmetadata_client(cfg) as (client, _token):
        base = f"activations/{stage}"
        publisher = QualityPublisher(
            store=s3,
            base_uri=f"s3://{bucket}/{base}",
            stage=stage,
            catalog=QualityCatalog(client),
            database_fqn=database_fqn,
        )

        def put(run: str, at: datetime, results: list[QualityResult]) -> str:
            manifest = QualityManifest(stage, domain.name, product.id, run, tuple(results), complete=True)
            key = f"{base}/{manifest.key}"
            s3.put_object(Bucket=bucket, Key=key, Body=manifest.to_json().encode(), ContentType="application/json")
            return key

        def result(check: str, engine: str, table: str, outcome: QualityOutcome, run: str, at: datetime):
            blocked = PipelineOutcome.BLOCKED if outcome is QualityOutcome.ERROR else PipelineOutcome.CONTINUED
            return QualityResult(
                engine=engine,
                stage=stage,
                domain=domain.name,
                product=product.id,
                table=table,
                check_id=check,
                engine_severity="abort" if outcome is QualityOutcome.ERROR else "warn",
                outcome=outcome,
                pipeline_outcome=blocked,
                observed_at=at,
                dagster_run_id=run,
                metrics={"violations_total": 2},
            )

        silver_table = entities[0].name
        keys: list[str] = []
        try:
            first = [
                result(WARN_RULE, "floe", silver_table, QualityOutcome.WARN, runs[0], start),
                result(ERROR_RULE, "floe", silver_table, QualityOutcome.ERROR, runs[0], start),
                result(DBT_RULE, "dbt", gold_table, QualityOutcome.PASSED, runs[0], start),
            ]
            keys.append(put(runs[0], start, first))
            report = publisher.reconcile(run_id=runs[0])
            if report.published != 3:
                raise E2EError(f"expected 3 published results, got {report}")
            for fqn, status in cases.items():
                history = _results(client, fqn)
                if [r["testCaseStatus"] for r in history] != [status]:
                    raise E2EError(f"{fqn}: expected one {status} result, OpenMetadata has {history}")
                if int(start.timestamp() * 1000) != history[0]["timestamp"]:
                    raise E2EError(f"{fqn}: the result is not stamped with the event time")
            warn_result = _results(client, f"{silver}.olf.floe.{WARN_RULE}")[0]
            warn_values = {v["name"]: v["value"] for v in warn_result["testResultValue"]}
            if warn_values.get("olf_outcome") != "warn":
                raise E2EError(f"a WARN lost its original outcome in OpenMetadata: {warn_values}")

            # Replay and a full pass leave the history as it was.
            publisher.reconcile(run_id=runs[0])
            publisher.reconcile()
            _expect_counts(client, cases, 1, "after a replay")

            # A second run is history on the same cases, not new cases.
            later = start + timedelta(minutes=5)
            second = [
                result(WARN_RULE, "floe", silver_table, QualityOutcome.PASSED, runs[1], later),
                result(ERROR_RULE, "floe", silver_table, QualityOutcome.PASSED, runs[1], later),
                result(DBT_RULE, "dbt", gold_table, QualityOutcome.ERROR, runs[1], later),
            ]
            keys.append(put(runs[1], later, second))
            if publisher.reconcile(run_id=runs[1]).published != 3:
                raise E2EError("the second run did not publish its three results")
            _expect_counts(client, cases, 2, "after a second run")
            listed = client.request(
                "GET", "/api/v1/dataQuality/testCases", params={"entityLink": f"<#E::table::{silver}>", "limit": 100}
            )
            names = sorted(c["name"] for c in listed.get("data") or [] if c["name"].startswith("olf.floe.e2e_"))
            if names != sorted([f"olf.floe.{WARN_RULE}", f"olf.floe.{ERROR_RULE}"]):
                raise E2EError(f"a second run changed the set of cases: {names}")

            # A table OpenMetadata does not know keeps its result pending, visibly, and creates nothing.
            unregistered = result(WARN_RULE, "floe", "zz_e2e_unregistered", QualityOutcome.WARN, runs[2], later)
            keys.append(put(runs[2], later, [unregistered]))
            blocked = publisher.reconcile(run_id=runs[2])
            summary = blocked.summary
            visible = summary is not None and summary.pending >= 1 and "not registered" in (summary.last_error or "")
            if blocked.deferred != 1 or not visible:
                raise E2EError(f"an unregistered table was not left pending and visible: {blocked}")
            _expect_absent(client, f"{database_fqn}.{domain.silver_namespace}.zz_e2e_unregistered")

            _expect_no_other_stage_writes(cfg, client, stage, database_fqn, domain.silver_namespace, entities[0].name)
        finally:
            for key in keys:
                for stored in (key, f"{base}/quality-sync/state/{domain.name}/{product.id}/{key.split('/')[-2]}.json"):
                    s3.delete_object(Bucket=bucket, Key=stored)
            for fqn in cases:
                _delete_case(client, fqn)
            publisher.reconcile(limit=1)
    _expect_clean_summary(cfg)


def _results(client: OpenMetadataClient, case_fqn: str) -> list[dict[str, Any]]:
    page = client.request("GET", RESULTS + urllib.parse.quote(case_fqn, safe=""))
    return sorted(page.get("data") or [], key=lambda r: r["timestamp"])


def _expect_counts(client: OpenMetadataClient, cases: dict[str, str], count: int, when: str) -> None:
    for fqn in cases:
        found = len(_results(client, fqn))
        if found != count:
            raise E2EError(f"{fqn}: expected {count} result(s) {when}, OpenMetadata has {found}")


def _expect_absent(client: OpenMetadataClient, table_fqn: str) -> None:
    try:
        client.request("GET", "/api/v1/tables/name/" + urllib.parse.quote(table_fqn, safe=""), ok_statuses=(404,))
    except OpenMetadataError as exc:
        raise E2EError(f"the publisher created or found a table it must not own: {table_fqn}: {exc}") from exc


def _expect_no_other_stage_writes(
    cfg: E2EConfig, client: OpenMetadataClient, stage: str, database_fqn: str, schema: str, table: str
) -> None:
    """A result of this stage never reaches another stage's root: its `olf.floe.e2e_*` cases do not exist there."""
    stages = load_provider_contracts_or_raise(cfg).get("stages") or {}
    service = database_fqn.split(".", 1)[0]
    for other in sorted(set(stages) - {stage}):
        fqn = f"{service}.lakehouse_{other}.{schema}.{table}.olf.floe.{WARN_RULE}"
        try:
            client.request(
                "GET", "/api/v1/dataQuality/testCases/name/" + urllib.parse.quote(fqn, safe=""), ok_statuses=(404,)
            )
        except OpenMetadataError as exc:
            raise E2EError(f"stage {stage}'s quality result reached stage {other}: {fqn}: {exc}") from exc


def _delete_case(client: OpenMetadataClient, fqn: str) -> None:
    try:
        client.request(
            "DELETE",
            "/api/v1/dataQuality/testCases/name/" + urllib.parse.quote(fqn, safe=""),
            params={"hardDelete": "true"},
            ok_statuses=(200, 404),
        )
    except OpenMetadataError as exc:
        log.error(f"could not remove the e2e test case {fqn}: {exc}")


def _expect_clean_summary(cfg: E2EConfig) -> None:
    """With the synthetic runs gone, the stage's sync lag signal has nothing pending: the summary is readable."""
    stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
    with ops_bucket_client(cfg) as (s3, bucket):
        body = s3.get_object(Bucket=bucket, Key=f"activations/{stage}/quality-sync/summary.json")["Body"].read()
    summary = SyncSummary.from_json(body.decode())
    if summary.stage != stage:
        raise E2EError(f"the sync summary is for stage {summary.stage}, expected {stage}")
