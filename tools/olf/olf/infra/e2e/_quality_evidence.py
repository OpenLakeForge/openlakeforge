"""Quality evidence of a real product run (#345, #346): its Dagster asset checks, its quality manifest and, with
governance, its OpenMetadata test case results.

`_quality_publication` proves the publisher with synthetic manifests. This module reads what a real product run left
behind and writes nothing but OpenMetadata results the reconciler would publish anyway. It runs before the alert
drill, which overwrites the latest run's manifest with synthetic outcomes. The log lines it writes are the evidence.
"""

from __future__ import annotations

import json
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from openlakeforge_core import Product
from openlakeforge_core.quality import (
    QualityManifest,
    QualityOutcome,
    QualityRecordError,
    QualityResult,
    manifest_key,
)
from openlakeforge_core.quality_sync import OmStatus, case_name, epoch_millis, om_status
from openlakeforge_runtime.floe_quality import FLOE_CHECK_IDS
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.quality_publisher import QualityCatalog, QualityPublisher

from olf.common import config, log
from olf.infra.e2e._artifacts import ops_bucket_client
from olf.infra.e2e._assertions import _openmetadata_e2e_login, openmetadata_client
from olf.infra.e2e._dagster import dagster_client
from olf.infra.e2e._layers import configured_layers
from olf.infra.e2e._quality_publication import (
    MAX_RECONCILER_PASSES,
    RESULTS,
    _reconciler_cronjob,
    _reconciler_paused,
    _run_reconciler_job,
    case_fqn,
)
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ
from olf.infra.e2e._trino import stage_catalog_name
from olf.infra.openmetadata._auth import admin_password
from olf.infra.openmetadata._quality_sync import schema_map

# A full product run: newer runs of the same job exist on Full (the Gold-only token-lifetime proof, the serialized
# pair), so the latest one that ran Silver is looked for among this many.
RUN_LOOKUP_LIMIT = 20
LOG_PAGE_SIZE = 500
FLOE_RULE_CHECKS = ("floe_cast_error", "floe_not_null", "floe_unique")
# Counts dagster-floe 0.6.1 puts on every Floe check from the run summary (`_extract_entity_stats`).
FLOE_COUNTS = ("rows", "accepted", "rejected")
# A result is found around its own timestamp; a day is the publisher's own identity window.
RESULT_WINDOW = timedelta(hours=1)

_RUNS = """
query ProductRuns($filter: RunsFilter, $limit: Int) {
  runsOrError(filter: $filter, limit: $limit) {
    __typename
    ... on Runs { results { runId status assetSelection { path } } }
    ... on InvalidPipelineRunsFilterError { message }
    ... on PythonError { message }
  }
}
"""

# The assets of the deployed job: its selection is `ProductDefinitionSpec.resolve()` as the code location ran it, so
# its Silver keys are the product's Silver inputs. olf cannot import the spec itself (it has no Dagster).
_JOB_ASSETS = """
query JobAssets($selector: PipelineSelector!) {
  assetNodes(pipeline: $selector) { assetKey { path } }
}
"""

_CHECK_EVALUATIONS = """
query RunCheckEvaluations($runId: ID!, $cursor: String, $limit: Int) {
  logsForRun(runId: $runId, afterCursor: $cursor, limit: $limit) {
    __typename
    ... on EventConnection {
      cursor
      hasMore
      events {
        __typename
        ... on AssetCheckEvaluationEvent {
          evaluation {
            checkName
            success
            severity
            assetKey { path }
            metadataEntries {
              label
              ... on IntMetadataEntry { intRepr }
              ... on FloatMetadataEntry { floatValue }
              ... on BoolMetadataEntry { boolValue }
              ... on TextMetadataEntry { text }
              ... on PathMetadataEntry { path }
              ... on UrlMetadataEntry { url }
              ... on JsonMetadataEntry { jsonString }
            }
          }
        }
      }
    }
    ... on RunNotFoundError { message }
    ... on PythonError { message }
  }
}
"""


@dataclass(frozen=True)
class CheckEvaluation:
    """One asset check evaluation a run recorded, its metadata as plain values."""

    asset: tuple[str, ...]
    name: str
    passed: bool
    severity: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def outcome(self) -> QualityOutcome:
        if self.passed:
            return QualityOutcome.PASSED
        return QualityOutcome.WARN if self.severity == "WARN" else QualityOutcome.ERROR


@dataclass(frozen=True)
class RunChecks:
    """A run's checks: per Silver table its five Floe checks, and the dbt checks on Gold."""

    silver: Mapping[str, Mapping[str, CheckEvaluation]]
    dbt: Mapping[str, CheckEvaluation]


def check_real_quality_evidence(cfg: E2EConfig, product: Product | None = None) -> None:
    """The latest full run of the product's job: five Floe checks with counts and a report on each Silver table,
    dbt checks on Gold, a complete manifest that agrees with them and, with governance, one OpenMetadata result per
    case that a second reconciler pass does not duplicate."""
    product = product or cfg.inventory.default_product
    stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
    log.step(f"Checking the quality evidence of a real {product.job_name} run...")
    with dagster_client(cfg) as client:
        silver_tables = product_silver_tables(client, product)
        if not silver_tables:
            log.info(
                f"{product.id} reads no {product.domain_name} Silver table, only other products' public marts: "
                "its runs have no Floe checks, so the Floe evidence is skipped."
            )
        run_id = latest_full_run(client, product, needs_silver=bool(silver_tables))
        checks = assert_run_checks(read_check_evaluations(client, run_id), product, silver_tables)
    with ops_bucket_client(cfg) as (s3, bucket):
        key = f"activations/{stage}/{manifest_key(product.domain_name, product.id, run_id)}"
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise E2EError(f"run {run_id} has no readable quality manifest at s3://{bucket}/{key}: {exc}") from exc
        try:
            manifest = QualityManifest.from_json(body.decode())
        except QualityRecordError as exc:
            raise E2EError(f"run {run_id}'s quality manifest is invalid: {exc}") from exc
        dbt_uri = dbt_manifest_uri(manifest)
        if dbt_uri is None:
            raise E2EError(f"run {run_id}'s dbt records point at no run_results.json to find its dbt manifest beside")
        dbt_bucket, dbt_key = _split_s3(dbt_uri)
        try:
            dbt_manifest = json.loads(s3.get_object(Bucket=dbt_bucket, Key=dbt_key)["Body"].read())
        except Exception as exc:
            raise E2EError(f"run {run_id}'s dbt manifest {plain_uri(dbt_uri)} is not readable: {exc}") from exc
    assert_manifest(manifest, checks, stage=stage, run_id=run_id, gold_tables=model_tables(dbt_manifest))
    log.info(f"Quality evidence of {product.job_name} run {run_id} in stage {stage}:")
    for line in evidence_lines(checks, manifest, product):
        log.info(f"  {line}")
    if not configured_layers(cfg)["governance"]:
        log.info("Governance is off for this stage; the run's evidence is not published to OpenMetadata.")
        return
    _check_openmetadata_results(cfg, stage, product, manifest, floe=bool(silver_tables))


def product_silver_tables(client: DagsterClient, product: Product) -> tuple[str, ...]:
    """The domain Silver tables the deployed job selects: none for a product reading only other products' marts."""
    try:
        location, repository = client.discover_repository(product.job_name)
    except ServiceClientError as exc:
        raise E2EError(f"Dagster has no loaded {product.job_name}: {exc}") from exc
    selector = {"pipelineName": product.job_name, "repositoryName": repository, "repositoryLocationName": location}
    nodes = client.graphql(_JOB_ASSETS, {"selector": selector}).get("assetNodes") or []
    if not nodes:
        raise E2EError(f"Dagster lists no assets for {product.job_name}")
    return job_silver_tables([tuple(node["assetKey"]["path"]) for node in nodes], product.domain_name)


def job_silver_tables(assets: Iterable[tuple[str, ...]], domain: str) -> tuple[str, ...]:
    return tuple(sorted({asset[1] for asset in assets if _is_silver(asset, domain)}))


def latest_full_run(client: DagsterClient, product: Product, *, needs_silver: bool = True) -> str:
    """The newest successful run of the job, one that selected Silver when the product reads any: a Gold-only
    selection has no Floe checks."""
    result = client.graphql(
        _RUNS, {"filter": {"pipelineName": product.job_name, "statuses": ["SUCCESS"]}, "limit": RUN_LOOKUP_LIMIT}
    )["runsOrError"]
    if result.get("__typename") != "Runs":
        raise E2EError(f"Dagster could not list {product.job_name} runs: {result.get('message', result)}")
    run_id = pick_full_run(result.get("results") or [], product.domain_name, needs_silver=needs_silver)
    if run_id is None:
        wanted = "that selected its Silver tables" if needs_silver else "at all"
        raise E2EError(f"{product.job_name} has no successful run {wanted}")
    return run_id


def pick_full_run(runs: Sequence[Mapping[str, Any]], domain: str, *, needs_silver: bool = True) -> str | None:
    for run in runs:
        if run.get("status") != "SUCCESS":
            continue
        selection = run.get("assetSelection")
        if not needs_silver or selection is None or any(_is_silver(tuple(key["path"]), domain) for key in selection):
            return str(run["runId"])
    return None


def read_check_evaluations(client: DagsterClient, run_id: str) -> list[CheckEvaluation]:
    pages: list[Mapping[str, Any]] = []
    cursor: str | None = None
    while True:
        page = client.graphql(_CHECK_EVALUATIONS, {"runId": run_id, "cursor": cursor, "limit": LOG_PAGE_SIZE})[
            "logsForRun"
        ]
        if page.get("__typename") != "EventConnection":
            raise E2EError(f"Dagster could not read run {run_id}'s events: {page.get('message', page)}")
        pages.append(page)
        if not page.get("hasMore") or page.get("cursor") in (None, cursor):
            return parse_check_evaluations(pages)
        cursor = page["cursor"]


def parse_check_evaluations(pages: Iterable[Mapping[str, Any]]) -> list[CheckEvaluation]:
    evaluations: list[CheckEvaluation] = []
    for page in pages:
        for event in page.get("events") or []:
            if event.get("__typename") != "AssetCheckEvaluationEvent":
                continue
            evaluation = event["evaluation"]
            evaluations.append(
                CheckEvaluation(
                    asset=tuple(evaluation["assetKey"]["path"]),
                    name=str(evaluation["checkName"]),
                    passed=bool(evaluation["success"]),
                    severity=str(evaluation["severity"]),
                    metadata={e["label"]: _metadata_value(e) for e in evaluation.get("metadataEntries") or []},
                )
            )
    return evaluations


def assert_run_checks(
    evaluations: Sequence[CheckEvaluation], product: Product, silver_tables: Sequence[str]
) -> RunChecks:
    """Every Silver table the product reads (`silver_tables`, none for a product on other products' marts alone)
    carries exactly the five Floe checks, once each, with counts and a report; no other Silver table is checked;
    Gold carries the dbt checks, and no check is evaluated twice."""
    domain = product.domain_name
    repeated = sorted(
        f"{'/'.join(a)}:{n}" for (a, n), c in Counter((e.asset, e.name) for e in evaluations).items() if c > 1
    )
    if repeated:
        raise E2EError(f"checks evaluated more than once in one run: {repeated}")
    silver: dict[str, dict[str, CheckEvaluation]] = {}
    dbt: dict[str, CheckEvaluation] = {}
    problems: list[str] = []
    for evaluation in evaluations:
        key = "/".join(evaluation.asset)
        if _is_silver(evaluation.asset, domain):
            silver.setdefault(evaluation.asset[1], {})[evaluation.name] = evaluation
        elif evaluation.name in FLOE_CHECK_IDS:
            problems.append(f"Floe check {evaluation.name} on {key}, which is not a {domain} Silver table")
        elif evaluation.asset[:1] == (product.id,):
            if evaluation.name in dbt:
                problems.append(f"dbt check {evaluation.name} on both {'/'.join(dbt[evaluation.name].asset)} and {key}")
            dbt[evaluation.name] = evaluation
        else:
            problems.append(f"check {evaluation.name} on {key}, which is neither {domain} Silver nor {product.id} Gold")
    if silver_tables and not silver:
        problems.append(f"no {domain} Silver table has a Floe check")
    if not dbt:
        problems.append(f"no {product.id} Gold table has a dbt check")
    problems.extend(
        f"{domain}/{table} has checks, but {product.id} does not read it"
        for table in sorted(set(silver) - {*silver_tables})
    )
    for table in sorted(silver_tables) if silver else ():
        found = silver.get(table, {})
        if sorted(found) != sorted(FLOE_CHECK_IDS):
            problems.append(f"{domain}/{table} has checks {sorted(found)}, not the five Floe checks")
        for name, evaluation in sorted(found.items()):
            problems.extend(f"{domain}/{table} {name}: {p}" for p in _floe_check_problems(evaluation))
    if problems:
        raise E2EError("the run's Dagster checks are not the expected evidence:\n  " + "\n  ".join(problems))
    return RunChecks(silver=silver, dbt=dbt)


def _floe_check_problems(evaluation: CheckEvaluation) -> list[str]:
    meta = evaluation.metadata
    problems = [f"no {key} count" for key in FLOE_COUNTS if not _is_count(meta.get(key))]
    if evaluation.name in FLOE_RULE_CHECKS and not _is_count(meta.get("violations_total")):
        problems.append("no violations_total count")
    if not isinstance(meta.get("entity_report_uri"), str) or not meta["entity_report_uri"]:
        problems.append("no entity report reference")
    if meta.get("fallback") == "summary_only":
        problems.append("derived from the run summary only: the entity report was not read")
    return problems


def assert_manifest(
    manifest: QualityManifest,
    checks: RunChecks,
    *,
    stage: str,
    run_id: str,
    gold_tables: Mapping[str, str] | None = None,
) -> None:
    """The run's manifest is complete and says what Dagster says: five Floe records per Silver table checked, and a
    record with the same outcome for every dbt check, on the physical table of the model Dagster checked.

    Dagster keys a Gold asset by model name while a record names the relation dbt built, whose alias may differ, so
    `gold_tables` (model name -> `schema.relation`, from the run's dbt manifest) joins the two."""
    problems: list[str] = []
    if (manifest.stage, manifest.dagster_run_id) != (stage, run_id):
        problems.append(f"it is for stage {manifest.stage} run {manifest.dagster_run_id}")
    if not manifest.complete:
        problems.append("it is not complete")
    by_table: dict[str, list[QualityResult]] = {}
    for result in manifest.results:
        if result.engine == "floe":
            by_table.setdefault(result.table or "", []).append(result)
    if sorted(by_table) != sorted(checks.silver):
        problems.append(f"Floe records name tables {sorted(by_table)}, Dagster checked {sorted(checks.silver)}")
    for table, results in sorted(by_table.items()):
        names = sorted(r.check_id for r in results)
        if names != sorted(FLOE_CHECK_IDS):
            problems.append(f"{table} has Floe records {names}, not one per Floe check")
        for floe_record in results:
            evaluation = (checks.silver.get(table) or {}).get(floe_record.check_id)
            if evaluation is not None and floe_record.outcome is not evaluation.outcome:
                problems.append(
                    f"{table} {floe_record.check_id} is {floe_record.outcome.value}, "
                    f"Dagster says {evaluation.outcome.value}"
                )
    dbt = {r.check_id: r for r in manifest.results if r.engine == "dbt"}
    for name, evaluation in sorted(checks.dbt.items()):
        dbt_record = dbt.get(name)
        if dbt_record is None:
            problems.append(f"dbt check {name} has no record")
        elif dbt_record.outcome is not evaluation.outcome:
            problems.append(f"dbt {name} is {dbt_record.outcome.value}, Dagster says {evaluation.outcome.value}")
        elif not _same_table(dbt_record.table or "", evaluation.asset[-1], gold_tables or {}):
            expected = (gold_tables or {}).get(evaluation.asset[-1], f"*.{evaluation.asset[-1]}")
            problems.append(
                f"dbt {name} is recorded on {dbt_record.table}, Dagster checked {'/'.join(evaluation.asset)} "
                f"({expected})"
            )
    if problems:
        raise E2EError(f"run {run_id}'s quality manifest disagrees with the run:\n  " + "\n  ".join(problems))


def _same_table(recorded: str, model: str, gold_tables: Mapping[str, str]) -> bool:
    physical = gold_tables.get(model)
    return recorded == physical if physical else recorded.endswith(f".{model}")


def dbt_manifest_uri(manifest: QualityManifest) -> str | None:
    """The run's own dbt `manifest.json`, uploaded beside the `run_results.json` its dbt records point at."""
    for result in manifest.results:
        uri = result.artifact_uri or ""
        if result.engine == "dbt" and uri.endswith("/run_results.json"):
            return uri.removesuffix("run_results.json") + "manifest.json"
    return None


def model_tables(dbt_manifest: Mapping[str, Any]) -> dict[str, str]:
    """Model name -> the `schema.relation` dbt built it as, the relation being its alias when one is set: the naming
    the dbt quality adapter gives a record's table."""
    tables: dict[str, str] = {}
    for node in (dbt_manifest.get("nodes") or {}).values():
        if not isinstance(node, Mapping) or node.get("resource_type") != "model":
            continue
        name, schema = node.get("name"), node.get("schema")
        relation = node.get("alias") or name
        if isinstance(name, str) and isinstance(schema, str) and schema and isinstance(relation, str):
            tables[name] = f"{schema}.{relation}"
    return tables


def _split_s3(uri: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme != "s3" or not parts.netloc:
        raise E2EError(f"{plain_uri(uri)} is not an s3:// object")
    return parts.netloc, parts.path.lstrip("/")


def evidence_lines(checks: RunChecks, manifest: QualityManifest, product: Product) -> list[str]:
    """One line per check: outcome, counts and report reference. A URI loses any query, so a presigned link
    cannot reach the log."""
    records = {(r.engine, r.table or "", r.check_id): r for r in manifest.results}
    silver = f"{product.domain_name}_silver"
    lines: list[str] = []
    for table, found in sorted(checks.silver.items()):
        for name in FLOE_CHECK_IDS:
            meta = found[name].metadata
            counts = [f"{key}={meta.get(key)}" for key in FLOE_COUNTS]
            if name in FLOE_RULE_CHECKS:
                counts.append(f"violations={meta.get('violations_total')}")
            record = records.get(("floe", table, name))
            outcome = record.outcome.value if record else found[name].outcome.value
            report = plain_uri(str(meta.get("entity_report_uri") or ""))
            lines.append(f"{name} on {silver}.{table}: {outcome}, {', '.join(counts)}, report={report}")
    for name, evaluation in sorted(checks.dbt.items()):
        record = next((r for r in manifest.results if r.engine == "dbt" and r.check_id == name), None)
        failures = record.metrics.get("failures") if record else None
        report = plain_uri(record.artifact_uri or "") if record else ""
        table = record.table if record and record.table else "/".join(evaluation.asset)
        lines.append(f"dbt {name} on {table}: {evaluation.outcome.value}, failures={failures}, report={report}")
    return lines


def plain_uri(uri: str) -> str:
    """`uri` without credentials, query or fragment."""
    parts = urllib.parse.urlsplit(uri)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@dataclass(frozen=True)
class CaseTarget:
    """The OpenMetadata case one record publishes to, and the result it must hold for the run."""

    fqn: str
    timestamp: int
    run_id: str
    # What the publisher maps the record's outcome to: a result under any other status misreports the run.
    status: OmStatus


def case_targets(
    manifest: QualityManifest,
    *,
    database_fqn: str,
    silver_schemas: Mapping[str, str] | None,
    gold_schemas: Mapping[str, str] | None,
    warn_status: OmStatus = OmStatus.ABORTED,
    floe: bool = True,
) -> list[CaseTarget]:
    """One Floe case on its Silver table and one dbt case on its Gold table, table-level when the run has one; the
    dbt case alone when the product reads no Silver (`floe` false). The table FQNs are the provider contract's,
    built here independently of the publisher's own mapping."""
    publishable = [r for r in manifest.results if om_status(r.outcome) is not None and r.table]
    floe_records = sorted(
        (r for r in publishable if r.engine == "floe"), key=lambda r: (r.check_id != "floe_not_null", r.table or "")
    )
    dbt = sorted((r for r in publishable if r.engine == "dbt"), key=lambda r: (r.column is not None, r.check_id))
    if not dbt or (floe and not floe_records):
        wanted = "Floe and dbt result" if floe else "dbt result"
        raise E2EError(f"run {manifest.dagster_run_id} has no publishable {wanted}")
    silver_schema = (silver_schemas or {}).get(manifest.domain) or f"{database_fqn}.{manifest.domain}_silver"
    gold_schema = (gold_schemas or {}).get(manifest.product) or f"{database_fqn}.{manifest.product}_gold"
    cases = [(floe_records[0], silver_schema)] if floe else []
    targets = []
    for record, schema in (*cases, (dbt[0], gold_schema)):
        table = f"{schema}.{(record.table or '').rsplit('.', 1)[-1]}"
        # OpenMetadata names a column case after the column's FQN (`EntityLink.getFullyQualifiedFieldValue`).
        parent = f"{table}.{record.column}" if record.column else table
        status = om_status(record.outcome, warn_status=warn_status)
        if status is None:  # unreachable: `publishable` holds only records with a status
            raise E2EError(f"{record.check_id} has no OpenMetadata status")
        targets.append(
            CaseTarget(
                case_fqn(parent, case_name(record)), epoch_millis(record.observed_at), record.dagster_run_id, status
            )
        )
    return targets


def run_results(found: Iterable[Mapping[str, Any]], run_id: str) -> list[Mapping[str, Any]]:
    """The results among `found` that the run published, by the run id the publisher records on each."""
    return [
        r
        for r in found
        if any(v.get("name") == "dagster_run_id" and v.get("value") == run_id for v in r.get("testResultValue") or [])
    ]


def assert_one_result(target: CaseTarget, found: Sequence[Mapping[str, Any]], when: str) -> Mapping[str, Any]:
    if len(found) != 1:
        raise E2EError(
            f"{target.fqn}: expected one result for run {target.run_id} {when}, OpenMetadata has {len(found)}"
        )
    if found[0].get("timestamp") != target.timestamp:
        raise E2EError(
            f"{target.fqn}: the run's result is stamped {found[0].get('timestamp')}, "
            f"not the run's observation time {target.timestamp}"
        )
    if found[0].get("testCaseStatus") != target.status.value:
        raise E2EError(
            f"{target.fqn}: the run's result is {found[0].get('testCaseStatus')} {when}, "
            f"but its manifest outcome publishes as {target.status.value}"
        )
    return found[0]


def _check_openmetadata_results(
    cfg: E2EConfig, stage: str, product: Product, manifest: QualityManifest, *, floe: bool = True
) -> None:
    environ = cfg_environ(cfg)
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=environ)
    if not database_fqn:
        raise E2EError("OPENLAKEFORGE_CATALOG_DATABASE_FQN is unset; cannot locate this stage's database root.")
    silver_schemas = schema_map("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON", environ)
    gold_schemas = schema_map("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", environ)
    cronjob = _reconciler_cronjob(cfg, stage)
    warn_status = reconciler_warn_status(cronjob)
    targets = case_targets(
        manifest,
        database_fqn=database_fqn,
        silver_schemas=silver_schemas,
        gold_schemas=gold_schemas,
        warn_status=warn_status,
        floe=floe,
    )
    if not floe:
        log.info(f"  {product.id} reads no Silver: no Floe case to check in OpenMetadata, only the dbt case.")
    with ops_bucket_client(cfg) as (s3, bucket), openmetadata_client(cfg) as (client, token):
        password = (
            "" if token else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
        )
        _openmetadata_e2e_login(client, password)

        def results(target: CaseTarget) -> list[Mapping[str, Any]]:
            window = int(RESULT_WINDOW.total_seconds() * 1000)
            page = client.request(
                "GET",
                RESULTS + urllib.parse.quote(target.fqn, safe=""),
                params={"startTs": target.timestamp - window, "endTs": target.timestamp + window},
                ok_statuses=(200, 404),
            )
            return run_results(page.get("data") or [], target.run_id)

        if cronjob is not None:

            def reconcile() -> None:
                _run_reconciler_job(cfg, cronjob)

            with _reconciler_paused(cfg, cronjob):
                _publish_and_replay(targets, reconcile, results)
            return
        # The cloud roots deploy no reconciler CronJob yet; the publisher it would run is driven directly.
        publisher = QualityPublisher(
            store=s3,
            base_uri=f"s3://{bucket}/activations/{stage}",
            stage=stage,
            catalog=QualityCatalog(client),
            database_fqn=database_fqn,
            silver_schemas=silver_schemas,
            gold_schemas=gold_schemas,
            warn_status=warn_status,
        )
        _publish_and_replay(targets, lambda: publisher.reconcile(run_id=manifest.dagster_run_id), results)


def _publish_and_replay(
    targets: Sequence[CaseTarget],
    reconcile: Callable[[], object],
    results: Callable[[CaseTarget], list[Mapping[str, Any]]],
) -> None:
    """Passes until every target holds the run's result (a pass takes the oldest runs first, so an earlier backlog
    can fill one), then one more, which must add nothing."""
    for _ in range(MAX_RECONCILER_PASSES):
        reconcile()
        if all(results(t) for t in targets):
            break
    first = {t.fqn: assert_one_result(t, results(t), "after a reconciler pass") for t in targets}
    reconcile()
    for target in targets:
        assert_one_result(target, results(target), "after a second reconciler pass")
        log.info(
            f"  OpenMetadata case {target.fqn}: 1 result for run {target.run_id} "
            f"(status {first[target.fqn].get('testCaseStatus')}, expected {target.status.value}, "
            f"timestamp {target.timestamp}), still 1 after a second pass"
        )


def reconciler_warn_status(cronjob: Mapping[str, Any] | None) -> OmStatus:
    """The status the reconciler publishes a WARN as: its `OPENLAKEFORGE_QUALITY_WARN_STATUS`, else the publisher's
    default, which is also what a root without the CronJob uses."""
    pod = ((((cronjob or {}).get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
    for container in (pod.get("spec") or {}).get("containers") or []:
        for env in container.get("env") or []:
            if env.get("name") == "OPENLAKEFORGE_QUALITY_WARN_STATUS" and env.get("value"):
                try:
                    return OmStatus(env["value"])
                except ValueError as exc:
                    raise E2EError(
                        f"the reconciler's warn status {env['value']!r} is not an OpenMetadata status"
                    ) from exc
    return OmStatus.ABORTED


def _is_silver(asset: tuple[str, ...], domain: str) -> bool:
    return len(asset) == 2 and asset[0] == domain


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _metadata_value(entry: Mapping[str, Any]) -> Any:
    if "intRepr" in entry:
        try:
            return int(entry["intRepr"])
        except (TypeError, ValueError):
            return entry["intRepr"]
    if "jsonString" in entry:
        try:
            return json.loads(entry["jsonString"])
        except (TypeError, ValueError):
            return entry["jsonString"]
    for key in ("floatValue", "boolValue", "text", "path", "url"):
        if key in entry:
            return entry[key]
    return None
