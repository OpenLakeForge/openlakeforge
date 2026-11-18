"""OpenMetadata quality publication assertions (#797): real OpenMetadata, real ops bucket, synthetic manifests.

The manifests are written here instead of waiting for a pipeline to warn or fail, because the demo data passes.
They use check ids no engine emits and run ids no Dagster run has, so no real result or alert can collide with
them, and everything they create is removed afterwards.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.parse
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult
from openlakeforge_core.quality_sync import SyncSummary
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError
from openlakeforge_services.quality_publisher import QualityCatalog, QualityPublisher

from olf.common import config, log
from olf.infra.e2e._alerts import job_outcome
from olf.infra.e2e._artifacts import ops_bucket_client
from olf.infra.e2e._assertions import _openmetadata_e2e_login, openmetadata_client
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl, load_provider_contracts_or_raise
from olf.infra.e2e._trino import product_gold_tables, stage_catalog_name
from olf.infra.openmetadata._auth import admin_password
from olf.infra.openmetadata._quality_sync import schema_map

WARN_RULE = "e2e_warn_rule"
ERROR_RULE = "e2e_error_rule"
DBT_RULE = "e2e_dbt_rule"
CRON_RULE = "e2e_cron_rule"
RESULTS = "/api/v1/dataQuality/testCases/testCaseResults/"
RECONCILER = "quality-reconciler"  # the CronJob's name prefix (modules/governance/quality-reconciler)
JOB_TIMEOUT_SECONDS = 240


def check_quality_publication(cfg: E2EConfig) -> None:
    """Published results land on existing tables of this stage only, keep WARN and ERROR apart, add history on a
    second run, and are idempotent on replay; an unregistered table stays pending and visible. On a stack that
    deploys the reconciler, a Job made from its CronJob publishes a result too."""
    log.step("Checking quality publication to OpenMetadata...")
    stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
    cronjob = _reconciler_cronjob(cfg, stage)
    with _reconciler_paused(cfg, cronjob):
        _check_publication(cfg, stage, cronjob)


def _check_publication(cfg: E2EConfig, stage: str, cronjob: Mapping[str, Any] | None) -> None:
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
    environ = cfg_environ(cfg)
    # The contract's own schema mappings, as the replay command passes them: on AWS Glue the physical schemas are
    # stage-prefixed and are not `<domain>_silver`.
    silver_schemas = schema_map("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON", environ)
    gold_schemas = schema_map("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", environ)
    silver_schema = (silver_schemas or {}).get(domain.name) or f"{database_fqn}.{domain.silver_namespace}"
    gold_table = product_gold_tables(cfg, product)[0]
    gold_schema = (gold_schemas or {}).get(product.id) or f"{database_fqn}.{product.gold_namespace}"
    silver = f"{silver_schema}.{entities[0].name}"
    gold = f"{gold_schema}.{gold_table.rsplit('.', 1)[1]}"

    stamp = int(datetime.now(UTC).timestamp())
    runs = [f"olf-e2e-quality-{stamp}-{i}" for i in (1, 2, 3, 4)]
    start = datetime.now(UTC).replace(microsecond=0)
    cases = {
        case_fqn(silver, f"olf.floe.{WARN_RULE}"): "Aborted",
        case_fqn(silver, f"olf.floe.{ERROR_RULE}"): "Failed",
        case_fqn(gold, f"olf.dbt.{DBT_RULE}"): "Success",
    }

    with ops_bucket_client(cfg) as (s3, bucket), openmetadata_client(cfg) as (client, token):
        # Without SSO there is no automation bot and the client carries no token: log in as the admin, as the
        # governance-assets check does.
        password = (
            "" if token else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        )
        _openmetadata_e2e_login(client, password)
        base = f"activations/{stage}"
        publisher = QualityPublisher(
            store=s3,
            base_uri=f"s3://{bucket}/{base}",
            stage=stage,
            catalog=QualityCatalog(client),
            database_fqn=database_fqn,
            silver_schemas=silver_schemas,
            gold_schemas=gold_schemas,
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
                # The server must know the case by the name the publisher derived from its own response.
                client.request("GET", "/api/v1/dataQuality/testCases/name/" + urllib.parse.quote(fqn, safe=""))
                history = _results(client, fqn)
                if [r["testCaseStatus"] for r in history] != [status]:
                    raise E2EError(f"{fqn}: expected one {status} result, OpenMetadata has {history}")
                if int(start.timestamp() * 1000) != history[0]["timestamp"]:
                    raise E2EError(f"{fqn}: the result is not stamped with the event time")
            warn_result = _results(client, case_fqn(silver, f"olf.floe.{WARN_RULE}"))[0]
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
            _expect_absent(client, f"{silver_schema}.zz_e2e_unregistered")

            _expect_no_other_stage_writes(cfg, client, stage, silver)

            if cronjob is not None:
                scheduled = result(CRON_RULE, "floe", silver_table, QualityOutcome.PASSED, runs[3], later)
                keys.append(put(runs[3], later, [scheduled]))
                _run_reconciler_job(cfg, cronjob)
                history = _results(client, case_fqn(silver, f"olf.floe.{CRON_RULE}"))
                if [r["testCaseStatus"] for r in history] != ["Success"]:
                    raise E2EError(f"the reconciler's Job did not publish the run's result: {history}")
        finally:
            for key in keys:
                for stored in (key, f"{base}/quality-sync/state/{domain.name}/{product.id}/{key.split('/')[-2]}.json"):
                    s3.delete_object(Bucket=bucket, Key=stored)
            for fqn in (*cases, case_fqn(silver, f"olf.floe.{CRON_RULE}")):
                _delete_case(client, fqn)
            publisher.reconcile(limit=1)
    _expect_clean_summary(cfg)


def _reconciler_cronjob(cfg: E2EConfig, stage: str) -> dict[str, Any] | None:
    """The stage's reconciler CronJob. The local root deploys one for every governed stage, so its absence there is
    a failure; the cloud roots do not deploy it yet, and are checked through the publisher alone."""
    listing = kubectl(
        cfg,
        ["get", "cronjob", f"{RECONCILER}-{stage}", "-n", cfg.platform_namespace, "--ignore-not-found", "-o", "json"],
        capture=True,
    )
    if listing.strip():
        found: dict[str, Any] = json.loads(listing)
        return found
    if cfg.env == "local":
        raise E2EError(f"stage {stage} has governance but no {RECONCILER}-{stage} CronJob in {cfg.platform_namespace}")
    log.info(f"No {RECONCILER}-{stage} CronJob on this stack; the scheduled pass is not checked.")
    return None


@contextlib.contextmanager
def _reconciler_paused(cfg: E2EConfig, cronjob: Mapping[str, Any] | None) -> Iterator[None]:
    """The CronJob suspended, with no scheduled pass still going, so it cannot publish the synthetic manifests
    before the checks read them; its own setting is put back."""
    if cronjob is None:
        yield
        return
    name, namespace = cronjob["metadata"]["name"], cronjob["metadata"]["namespace"]

    def suspend(value: bool) -> None:
        kubectl(cfg, ["patch", "cronjob", name, "-n", namespace, "-p", json.dumps({"spec": {"suspend": value}})])

    def active() -> bool:
        live = json.loads(kubectl(cfg, ["get", "cronjob", name, "-n", namespace, "-o", "json"], capture=True))
        return bool(live.get("status", {}).get("active"))

    was = bool(cronjob["spec"].get("suspend"))
    suspend(True)
    try:
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while active():
            if time.monotonic() >= deadline:
                raise E2EError(f"a scheduled {name} pass is still active")
            time.sleep(3)
        yield
    finally:
        suspend(was)


def _run_reconciler_job(cfg: E2EConfig, cronjob: Mapping[str, Any]) -> None:
    """One pass of the CronJob's own pod template to completion: the image, key, Secrets and NetworkPolicy of the
    scheduled run, without waiting for its schedule."""
    cronjob_name, namespace = cronjob["metadata"]["name"], cronjob["metadata"]["namespace"]
    name = f"{cronjob_name}-e2e-{uuid.uuid4().hex[:6]}"
    kubectl(cfg, ["create", "job", name, f"--from=cronjob/{cronjob_name}", "-n", namespace])
    try:
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while (outcome := job_outcome(_job(cfg, name, namespace))) is None:
            if time.monotonic() >= deadline:
                raise E2EError(f"{name} did not finish in {JOB_TIMEOUT_SECONDS}s")
            time.sleep(3)
        if outcome == "failed":
            tail = kubectl(cfg, ["logs", f"job/{name}", "-n", namespace, "--tail=30"], capture=True)
            raise E2EError(f"{name} failed:\n{tail}")
    finally:
        kubectl(cfg, ["delete", "job", name, "-n", namespace, "--ignore-not-found", "--wait=false"])


def _job(cfg: E2EConfig, name: str, namespace: str) -> dict[str, Any]:
    job: dict[str, Any] = json.loads(kubectl(cfg, ["get", "job", name, "-n", namespace, "-o", "json"], capture=True))
    return job


def case_fqn(table_fqn: str, name: str) -> str:
    """OpenMetadata 1.13.6's name for a case: `FullyQualifiedName.add` quotes a segment containing a dot."""
    return f'{table_fqn}."{name}"'


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


def _expect_no_other_stage_writes(cfg: E2EConfig, client: OpenMetadataClient, stage: str, silver: str) -> None:
    """A result of this stage never reaches another stage's root: its `olf.floe.e2e_*` cases do not exist there.
    The other stage's table name is this one's with the stage's catalog swapped, which also holds for prefixed
    physical schemas."""
    stages = load_provider_contracts_or_raise(cfg).get("stages") or {}
    for other in sorted(set(stages) - {stage}):
        other_table = silver.replace(f"lakehouse_{stage}", f"lakehouse_{other}")
        fqn = case_fqn(other_table, f"olf.floe.{WARN_RULE}")
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
            params={"hardDelete": "true", "recursive": "true"},
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
