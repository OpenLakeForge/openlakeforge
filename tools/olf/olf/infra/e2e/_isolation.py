"""Cross-stage isolation probes.

Proves the acceptance criterion #114 exists for: "DEV runtime configuration
cannot read/write PROD, and vice versa." Every probe is paired with a
positive control against the caller's own stage, so the suite cannot pass
merely because the cluster is broken rather than because isolation holds.

Each stage's suite probes every other enabled stage (#251), so the suites of
an N-stage deployment together cover every ordered pair of stages. Each suite
records what it found in `.tmp/e2e-evidence/stage-isolation-<namespace>.json`.

Local-only today: local is the only stage-aware root with more than one
stage actually provisioned in this repository's verification profile. A
deployment with only one enabled stage has nothing to isolate against, so
this module skips cleanly rather than failing.
"""

from __future__ import annotations

import functools
import itertools
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from openlakeforge_services.base import ServiceClientError

from olf.common import config, log
from olf.infra import k8s, trino
from olf.infra.e2e._dagster import DAGSTER_WEBSERVER_SERVICE_NAME
from olf.infra.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise
from olf.infra.e2e._trino import PROBE_SCHEMA, list_tables, processing_credential, trino_query

ProbeStatus = Literal["passed", "failed", "deferred", "skipped"]
REPORT_SCHEMA_VERSION = 1
# The key a cross-stage write probe would leave if it were not refused; it is deleted again when it is not.
WRITE_PROBE_KEY = "zz-olf-e2e-isolation-probe"


@dataclass(frozen=True)
class NotCompared:
    """A probe that could not compare a pair: `deferred` until the other stage has state of that kind (its own
    suite then compares the pair), `skipped` when this stage holds no identity of that kind."""

    status: Literal["deferred", "skipped"]
    reason: str


@dataclass(frozen=True)
class ProbeResult:
    stage: str
    other_stage: str
    probe: str
    status: ProbeStatus
    detail: str | None = None


@dataclass
class IsolationReport:
    """What one stage's suite found against every other enabled stage. A failing probe is recorded and the rest
    still run, so one breach does not hide the others."""

    stage: str
    enabled_stages: list[str]
    results: list[ProbeResult] = field(default_factory=list)

    def run(self, other: str, probe: str, call: Callable[[], NotCompared | None]) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        log.step(f"Checking {probe} isolation ({self.stage} -> {other})...")
        try:
            outcome = call()
        # Not only E2EError: a port-forward to a stage whose service has no Ready endpoint raises KubectlError, and
        # letting it escape would skip the remaining pairs and leave this probe out of the report.
        except (E2EError, k8s.KubectlError, ServiceClientError, BotoCoreError, ClientError, OSError) as exc:
            log.error(f"{probe} isolation ({self.stage} -> {other}): {exc}")
            self.results.append(ProbeResult(self.stage, other, probe, "failed", str(exc)))
            return
        if outcome is None:
            self.results.append(ProbeResult(self.stage, other, probe, "passed"))
        else:
            self.results.append(ProbeResult(self.stage, other, probe, outcome.status, outcome.reason))

    def failures(self) -> list[ProbeResult]:
        return [result for result in self.results if result.status == "failed"]

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "check": "stage-isolation",
            "stage": self.stage,
            "enabled_stages": self.enabled_stages,
            "pairs": [list(pair) for pair in ordered_stage_pairs(self.enabled_stages) if pair[0] == self.stage],
            "results": [asdict(result) for result in self.results],
        }


def ordered_stage_pairs(stages: Iterable[str]) -> list[tuple[str, str]]:
    """Every ordered (A, B) pair of distinct stages: A's identities must not reach B's state. A stage's suite
    probes the pairs it comes first in, so the suites of all enabled stages together cover every pair."""
    return list(itertools.permutations(sorted(set(stages)), 2))


def _other_enabled_stages(cfg: E2EConfig, stages: Iterable[str]) -> list[str]:
    """The contract's other stages whose namespace exists. A contract file exported before a stage was removed
    still names it, and a probe of a namespace that is gone would fail as a connection error, not a breach."""
    this_stage = cfg.namespace.removeprefix("olf-")
    others = []
    for stage in sorted(set(stages) - {this_stage}):
        if k8s.resource_exists("namespace", f"olf-{stage}", f"olf-{stage}", environ=cfg.environ):
            others.append(stage)
        else:
            log.info(f"Stage {stage} is in the provider contract but olf-{stage} does not exist; not probed.")
    return others


def _expect_trino_denied(cfg: E2EConfig, *, user: str, sql: str, what: str, sibling: str) -> None:
    """Require the query to fail because this stage's Trino cannot see the sibling's catalog.

    Each stage's coordinator holds only its own catalogs (ADR 0019 D8),
    so the sibling's is either hidden by the access rules or absent: Trino
    answers "Access Denied" or "Catalog ... not found". `trino_query` raises
    the same `E2EError` for those and for an unrelated failure (a broken pod,
    a transient connection error, ...), so only those two messages count.
    """
    try:
        trino_query(cfg, sql)
    except E2EError as exc:
        text = str(exc).lower()
        if "access denied" not in text and f"catalog 'lakehouse_{sibling}' not found" not in text:
            raise E2EError(
                f"isolation probe for {user} could not verify a denial of {what}: the query failed for a "
                f"reason other than an access-control rejection ({exc})."
            ) from exc
        return
    raise E2EError(f"isolation breach: {user} was not denied {what} ({sql!r} succeeded).")


# Trino reports Polaris's 403 on a table as a generic "Failed to load table" and keeps the reason in the cause
# chain; the paired control just loaded that table with the stage's own token, so it is not a missing table.
POLARIS_REFUSAL_MARKERS = ("forbidden", "not authorized", "403", "failed to load table")


def _expect_trino_refused(cfg: E2EConfig, sql: str, *, what: str, markers: tuple[str, ...] = (), **kw: Any) -> str:
    """Require `sql` to fail, and with `markers`, to fail as one of them; return the refusal."""
    try:
        trino_query(cfg, sql, **kw)
    except E2EError as exc:
        text = str(exc)
        if markers and not any(marker in text.lower() for marker in markers):
            raise E2EError(f"{what} failed, but not as a Polaris refusal ({exc}).") from exc
        return text
    raise E2EError(f"isolation breach: {what} succeeded ({sql!r}).")


def check_keyless_trino(cfg: E2EConfig, provider_contracts: dict[str, Any], this_stage: str, sibling: str) -> None:
    """Polaris decides every table access on the keyless catalog, from the token the caller sends (#964, #982).

    Each refusal is paired with the same statement succeeding with this stage's own token, so a broken
    coordinator cannot pass for a refusal.
    """
    catalog = f"lakehouse_{this_stage}"
    tables = [table for layer, table in _materialized_tables(cfg, catalog) if layer == "gold"]
    if not tables:
        raise E2EError(f"no Gold table in {catalog} to check the keyless catalog against.")
    select = f"SELECT count(*) FROM {catalog}.{tables[0]}"
    log.step(f"Checking the keyless {catalog} refuses a call with no token or {sibling}'s token...")
    trino_query(cfg, select)
    _expect_trino_refused(cfg, select, what="a query with no Polaris token", credential=trino.decode_credential(""))
    sibling_catalog = provider_contracts["stages"][sibling]["catalog"]
    lookup = trino.processing_credential_args(
        {
            "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME": sibling_catalog["floe_credentials_secret_name"],
            "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY": sibling_catalog["floe_client_id_key"],
            "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY": sibling_catalog["floe_client_secret_key"],
        },
        f"olf-{sibling}",
    )
    assert lookup is not None
    _expect_trino_refused(
        cfg,
        select,
        what=f"{sibling}'s processing token on {catalog}",
        markers=POLARIS_REFUSAL_MARKERS,
        credential=processing_credential(cfg, lookup),
    )

    log.step(f"Checking {catalog} refuses a table located in {sibling}'s storage...")
    schema = f"{catalog}.{PROBE_SCHEMA}"
    trino_query(cfg, f"CREATE SCHEMA IF NOT EXISTS {schema}")
    elsewhere = f"s3://{_stage_bucket(provider_contracts, sibling, 'silver')}/zz-olf-e2e-location-probe"
    for sql, what in (
        (
            f"CREATE TABLE {schema}.location_probe (probe integer) WITH (location = '{elsewhere}')",
            "a location override",
        ),
        (
            f"CALL {catalog}.system.register_table(schema_name => '{PROBE_SCHEMA}', "
            f"table_name => 'register_probe', table_location => '{elsewhere}')",
            "register_table",
        ),
    ):
        try:
            _expect_trino_refused(cfg, sql, what=f"{what} into {sibling}'s bucket")
        finally:
            for table in ("location_probe", "register_probe"):
                trino_query(cfg, f"DROP TABLE IF EXISTS {schema}.{table}")


def _expect_s3_denied(call, *, who: str, what: str) -> None:  # noqa: ANN001
    """Require the call to fail specifically with an access denial.

    `HeadBucket` has no response body to carry a symbolic AWS error code, so
    botocore reports its HTTP status code (e.g. "403", "404") as `Error.Code`
    instead - checking the HTTP status directly is what actually
    distinguishes "denied" from "the sibling bucket does not exist" for both
    that and `ListObjectsV2`, whose body-bearing error does use a symbolic
    code like "AccessDenied".
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        call()
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code")
        if status != 403 and code not in {"403", "AccessDenied", "Forbidden"}:
            raise E2EError(
                f"isolation probe for {who} could not verify a denial of {what}: the call failed for a "
                f"reason other than access control ({exc})."
            ) from exc
    except BotoCoreError as exc:
        raise E2EError(
            f"isolation probe for {who} could not verify a denial of {what}: the call failed for a "
            f"reason other than access control ({exc})."
        ) from exc
    else:
        raise E2EError(f"isolation breach: {who}'s S3 identity was not denied {what}.")


def _s3_identity(
    namespace: str, secret_name: str, *, environ: Mapping[str, str] | None = None
) -> tuple[str, str] | None:
    try:
        access_key = k8s.secret_value(secret_name, "AWS_ACCESS_KEY_ID", namespace, environ=environ)
        secret_key = k8s.secret_value(secret_name, "AWS_SECRET_ACCESS_KEY", namespace, environ=environ)
    except k8s.KubectlError:
        return None
    return access_key, secret_key


def _s3_client(access_key: str, secret_key: str, *, local_port: int, region: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{local_port}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(s3={"addressing_style": "path"}),
    )


def _stage_bucket(provider_contracts: dict[str, Any], stage: str, layer: str) -> str:
    try:
        bucket = provider_contracts["stages"][stage]["storage"][layer]["bucket_name"]
    except (KeyError, TypeError) as exc:
        raise E2EError(f"provider_contracts.stages.{stage}.storage.{layer}.bucket_name is required.") from exc
    if not isinstance(bucket, str) or not bucket:
        raise E2EError(f"provider_contracts.stages.{stage}.storage.{layer}.bucket_name must be a non-empty string.")
    return bucket


def _materialized_tables(cfg: E2EConfig, catalog: str) -> list[tuple[str, str]]:
    """(layer, schema.table) for every table Trino lists in a Silver or Gold schema."""
    schemas = [("silver", domain.silver_namespace) for domain in cfg.inventory.domains]
    schemas += [("gold", product.gold_namespace) for product in cfg.inventory.products]
    return sorted(
        (layer, f"{schema}.{table}") for layer, schema in schemas for table in list_tables(cfg, schema, catalog=catalog)
    )


def _check_table_locations(cfg: E2EConfig, provider_contracts: dict[str, Any], stage: str) -> None:
    """Every Silver and Gold table has data files, all in the stage's own
    bucket for its layer."""
    catalog = f"lakehouse_{stage}"
    for layer, table in _materialized_tables(cfg, catalog):
        expected = _stage_bucket(provider_contracts, stage, layer)
        schema, name = table.split(".", 1)
        paths = {
            line.strip()
            for line in trino_query(cfg, f'SELECT file_path FROM {catalog}.{schema}."{name}$files"').splitlines()
            if line.strip()
        }
        if not paths:
            raise E2EError(f"{catalog}.{table} has no data files to attribute to a stage.")
        buckets = {path.split("://", 1)[-1].split("/", 1)[0] for path in paths}
        if buckets != {expected}:
            raise E2EError(
                f"{catalog}.{table} reads data files from {sorted(buckets)}; only {stage}'s {layer} "
                f"bucket {expected!r} is allowed."
            )


def stage_bucket_objects(
    provider_contracts: dict[str, Any],
    stage: str,
    layers: tuple[str, ...],
    *,
    namespace: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """`<bucket>/<key>` -> ETag for every object in a stage's buckets for
    `layers`, listed with the platform's S3 identity (the stage identities are
    denied each other's). Also the nightly's before/after view of a promotion,
    which must leave the target stage's buckets untouched."""
    items = stage_bucket_items(provider_contracts, stage, layers, namespace=namespace, environ=environ)
    return {path: item["ETag"] for path, item in items.items()}


def stage_bucket_items(
    provider_contracts: dict[str, Any],
    stage: str,
    layers: tuple[str, ...],
    *,
    namespace: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Mapping[str, Any]]:
    """`<bucket>/<key>` -> the S3 listing entry (`ETag`, `Size`, ...) of every object `stage_bucket_objects` covers."""
    from botocore.exceptions import BotoCoreError, ClientError

    identity = _s3_identity(namespace, "seaweedfs-s3-creds", environ=environ)
    if identity is None:
        raise E2EError(f"no-copy probe: seaweedfs-s3-creds not found in {namespace}.")
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge", environ=environ)
    objects: dict[str, Mapping[str, Any]] = {}
    with k8s.port_forward(
        "seaweedfs-s3", 8333, namespace, log_path=f"{log_prefix}-isolation-s3-admin.log", environ=environ
    ) as port:
        region = config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1", environ=environ)
        client = _s3_client(*identity, local_port=port, region=region)
        for layer in layers:
            bucket = _stage_bucket(provider_contracts, stage, layer)
            try:
                for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                    objects.update({f"{bucket}/{item['Key']}": item for item in page.get("Contents", [])})
            except (BotoCoreError, ClientError) as exc:
                raise E2EError(f"could not list {stage}'s {layer} bucket {bucket!r}: {exc}") from exc
    return objects


def _bucket_data_files(cfg: E2EConfig, provider_contracts: dict[str, Any], stage: str) -> set[str]:
    """Parquet data file names in a stage's Silver and Gold buckets, registered
    or not -- a copied file need not be in any snapshot."""
    objects = stage_bucket_objects(
        provider_contracts,
        stage,
        ("silver", "gold"),
        namespace=cfg.shared_namespace or "olf-system",
        environ=cfg.environ,
    )
    return {path.rsplit("/", 1)[-1] for path in objects if path.endswith(".parquet")}


def check_stage_data_is_its_own(
    cfg: E2EConfig, provider_contracts: dict[str, Any], this_stage: str, sibling: str
) -> NotCompared | None:
    """Promotion copies no data: this stage's Silver and Gold hold only files
    this stage wrote.

    Location alone is not enough -- DEV files copied into PROD's bucket would
    sit where PROD's belong, whether or not a snapshot still references them.
    Iceberg names every data file uniquely per write and a copy keeps the
    name, so the two stages' buckets must share no data file name. A sibling
    that has written nothing yet (DEV's suite runs before promotion) is
    compared from its own suite. Requiring every table to have data files is
    the positive control.
    """
    log.step(f"Checking {this_stage} holds only data files {this_stage} wrote...")
    _check_table_locations(cfg, provider_contracts, this_stage)
    sibling_files = _bucket_data_files(cfg, provider_contracts, sibling)
    if not sibling_files:
        log.info(f"{sibling} has written no Silver or Gold data yet; its suite compares the pair.")
        return NotCompared("deferred", f"{sibling} has written no Silver or Gold data yet")
    copied = _bucket_data_files(cfg, provider_contracts, this_stage) & sibling_files
    if copied:
        raise E2EError(
            f"promotion copied data: {this_stage}'s buckets hold {len(copied)} data file(s) that also exist "
            f"in {sibling}'s, e.g. {sorted(copied)[:3]}."
        )
    return None


def _dagster_run_ids(
    service_name: str, namespace: str, log_path: str, *, environ: Mapping[str, str] | None = None
) -> set[str]:
    from openlakeforge_services.dagster import DagsterClient

    with k8s.port_forward(service_name, 80, namespace, log_path=log_path, environ=environ) as local_port:
        try:
            result = DagsterClient(f"http://127.0.0.1:{local_port}/graphql").graphql(
                "query { runsOrError(limit: 500) { __typename ... on Runs { results { runId } } } }"
            )["runsOrError"]
        except ServiceClientError as exc:
            raise E2EError(f"could not list Dagster runs in {namespace}: {exc}") from exc
    if result.get("__typename") != "Runs":
        raise E2EError(f"could not list Dagster runs in {namespace}: {result}")
    return {run["runId"] for run in result["results"]}


def check_dagster_state_isolation(cfg: E2EConfig, this_stage: str, sibling: str) -> NotCompared | None:
    """Each stage's Dagster instance sees only its own runs.

    Run IDs are UUIDs, so disjointness only means something when both sides
    have runs: two webservers reading one shared run store would then list the
    same runs and fail here. This stage having runs is the positive control; a
    sibling without any yet is compared later, from its own suite.
    """
    log.step(f"Checking Dagster run-state isolation ({this_stage} <-> {sibling})...")
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge", environ=cfg.environ)
    runs = {
        stage: _dagster_run_ids(
            DAGSTER_WEBSERVER_SERVICE_NAME,
            f"olf-{stage}",
            f"{log_prefix}-isolation-dagster-{stage}.log",
            environ=cfg.environ,
        )
        for stage in (this_stage, sibling)
    }
    if not runs[this_stage]:
        raise E2EError(f"{this_stage}'s Dagster has no runs; run-state isolation cannot be verified.")
    if not runs[sibling]:
        # The promotion order runs DEV's suite before PROD has run anything;
        # the pair is compared from PROD's suite, once both have history.
        log.info(f"{sibling}'s Dagster has no runs yet; its suite compares the pair.")
        return NotCompared("deferred", f"{sibling}'s Dagster has no runs yet")
    shared = runs[this_stage] & runs[sibling]
    if shared:
        raise E2EError(f"isolation breach: {this_stage} and {sibling} Dagster share runs {sorted(shared)[:5]}.")
    return None


_LAYERS = ("bronze", "silver", "gold")


def _expect_s3_allowed(call: Callable[[], object], *, who: str, what: str) -> None:
    """A positive control: without it a denial could be a broken identity or gateway, not isolation."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        call()
    except (BotoCoreError, ClientError) as exc:
        raise E2EError(f"positive control failed: {who} could not {what} ({exc}).") from exc


def _expect_s3_write_denied(client: Any, bucket: str, key: str, *, who: str, what: str) -> None:
    """Require PutObject to be refused; an object that was written anyway is deleted before the breach is reported,
    so a failing probe does not leave state in the other stage."""
    written: list[str] = []

    def put() -> None:
        client.put_object(Bucket=bucket, Key=key, Body=b"")
        written.append(key)

    try:
        _expect_s3_denied(put, who=who, what=what)
    finally:
        if written:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception as exc:  # the breach is what the run reports, not the cleanup
                log.error(f"could not remove the isolation write probe s3://{bucket}/{key}: {exc}")


def _expect_list_denied(client: Any, bucket: str, prefix: str, *, who: str) -> None:
    _expect_s3_denied(
        lambda: client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1),
        who=who,
        what=f"listing {prefix} in {bucket}",
    )


def _check_object_storage(
    client: Any, provider_contracts: dict[str, Any], ops_bucket: str, this_stage: str, other: str
) -> None:
    """`this_stage`'s S3 identity can neither read nor write `other`'s lakehouse buckets or its activation prefix
    in the shared ops bucket, and still reaches its own."""
    for layer in _LAYERS:
        bucket = _stage_bucket(provider_contracts, other, layer)
        _expect_s3_denied(
            functools.partial(client.head_bucket, Bucket=bucket),
            who=this_stage,
            what=f"HeadBucket on {other}'s {layer} bucket {bucket}",
        )
    other_bronze = _stage_bucket(provider_contracts, other, "bronze")
    _expect_s3_write_denied(
        client, other_bronze, f"{WRITE_PROBE_KEY}/{this_stage}", who=this_stage, what=f"PutObject in {other_bronze}"
    )
    _expect_list_denied(client, ops_bucket, f"activations/{other}/", who=this_stage)
    _expect_s3_write_denied(
        client,
        ops_bucket,
        f"activations/{other}/{WRITE_PROBE_KEY}",
        who=this_stage,
        what=f"PutObject under activations/{other}/ in the shared ops bucket",
    )
    for layer in _LAYERS:
        bucket = _stage_bucket(provider_contracts, this_stage, layer)
        _expect_s3_allowed(
            functools.partial(client.head_bucket, Bucket=bucket),
            who=this_stage,
            what=f"HeadBucket on its own {layer} bucket {bucket}",
        )
    _expect_s3_allowed(
        lambda: client.list_objects_v2(Bucket=ops_bucket, Prefix=f"activations/{this_stage}/", MaxKeys=1),
        who=this_stage,
        what=f"list its own activations/{this_stage}/",
    )


def _check_quality_state(
    cfg: E2EConfig, local_port: int, region: str, ops_bucket: str, this_stage: str, other: str
) -> NotCompared | None:
    """The stage's quality keys (ADR 0007, #1186) reach only the stage's own quality manifests and sync state: the
    reconciler's key, which also writes `quality-sync/`, and the platform API's read-only quality key."""
    namespace = cfg.shared_namespace or "olf-system"
    keys = (
        (f"quality-reconciler-{this_stage}", ("quality-sync/", "run-artifacts/quality/"), "quality-sync/"),
        (f"platform-api-quality-reader-{this_stage}", ("run-artifacts/quality/",), "run-artifacts/quality/"),
    )
    probed = []
    for name, denied, own in keys:
        identity = _s3_identity(namespace, f"{name}-s3-creds", environ=cfg.environ)
        if identity is None:
            continue
        probed.append(name)
        client = _s3_client(*identity, local_port=local_port, region=region)
        for prefix in denied:
            _expect_list_denied(client, ops_bucket, f"activations/{other}/{prefix}", who=name)
        if own == "quality-sync/":
            _expect_s3_write_denied(
                client,
                ops_bucket,
                f"activations/{other}/quality-sync/{WRITE_PROBE_KEY}",
                who=name,
                what=f"PutObject under activations/{other}/quality-sync/",
            )
        _expect_s3_allowed(
            functools.partial(
                client.list_objects_v2, Bucket=ops_bucket, Prefix=f"activations/{this_stage}/{own}", MaxKeys=1
            ),
            who=name,
            what=f"list its own activations/{this_stage}/{own}",
        )
    if not probed:
        return NotCompared("skipped", f"{this_stage} has no quality reconciler or quality reader key")
    return None


def _retain_report(cfg: E2EConfig, report: IsolationReport) -> None:
    path = cfg.repo_root / ".tmp/e2e-evidence" / f"stage-isolation-{cfg.namespace}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = report.as_json()
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    log.info("STAGE_ISOLATION_JSON " + json.dumps(record, sort_keys=True))


def _probe_every_other_stage(
    cfg: E2EConfig, provider_contracts: dict[str, Any], report: IsolationReport, others: list[str]
) -> None:
    this_stage = report.stage
    # Positive control: the stage's own identity reaches its own catalog, so a denial below is not a dead coordinator.
    trino_query(cfg, f"SELECT 1 FROM lakehouse_{this_stage}.information_schema.tables LIMIT 1")
    for other in others:
        report.run(
            other,
            "trino-catalog",
            functools.partial(
                _expect_trino_denied,
                cfg,
                user=f"{cfg.namespace}-runtime",
                sql=f"SELECT 1 FROM lakehouse_{other}.information_schema.tables LIMIT 1",
                what=f"read access to lakehouse_{other}",
                sibling=other,
            ),
        )
        report.run(
            other, "polaris-catalog", functools.partial(check_keyless_trino, cfg, provider_contracts, this_stage, other)
        )

    identity = _s3_identity(cfg.namespace, f"seaweedfs-{this_stage}-s3-creds", environ=cfg.environ)
    if identity is None:
        raise E2EError(f"stage isolation probe: seaweedfs-{this_stage}-s3-creds not found in {cfg.namespace}.")
    region = config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1", environ=cfg.environ)
    ops_bucket = config.env("OPENLAKEFORGE_OPS_BUCKET_NAME", "openlakeforge-ops", environ=cfg.environ)
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge", environ=cfg.environ)
    with k8s.port_forward(
        "seaweedfs-s3",
        8333,
        cfg.shared_namespace or "olf-system",
        log_path=f"{log_prefix}-isolation-s3.log",
        environ=cfg.environ,
    ) as local_port:
        client = _s3_client(*identity, local_port=local_port, region=region)
        for other in others:
            report.run(
                other,
                "object-storage",
                functools.partial(_check_object_storage, client, provider_contracts, ops_bucket, this_stage, other),
            )
            report.run(
                other,
                "quality-state",
                functools.partial(_check_quality_state, cfg, local_port, region, ops_bucket, this_stage, other),
            )

    for other in others:
        report.run(
            other,
            "stage-data",
            functools.partial(check_stage_data_is_its_own, cfg, provider_contracts, this_stage, other),
        )
        report.run(other, "dagster-runs", functools.partial(check_dagster_state_isolation, cfg, this_stage, other))


def check_stage_isolation(cfg: E2EConfig) -> None:
    """Prove this stage's identities cannot read or write any other enabled stage's Trino catalog, Polaris
    catalog, lakehouse buckets, ops-bucket activation prefix, quality state or Dagster run store -- and, as a
    positive control, that they still reach their own. Every pair is probed before the first failure is raised,
    and the report is written either way."""
    if cfg.env != "local":
        log.info(f"Skipping stage isolation probe: not yet wired for {cfg.env}.")
        return
    provider_contracts = load_provider_contracts_or_raise(cfg)
    others = _other_enabled_stages(cfg, provider_contracts.get("stages") or {})
    if not others:
        log.info("Skipping stage isolation probe: only one stage is enabled.")
        return

    this_stage = cfg.namespace.removeprefix("olf-")
    report = IsolationReport(this_stage, sorted({this_stage, *others}))
    try:
        _probe_every_other_stage(cfg, provider_contracts, report, others)
    finally:
        _retain_report(cfg, report)
    failures = report.failures()
    if failures:
        raise E2EError(
            f"stage isolation failed in {len(failures)} probe(s): "
            + "; ".join(f"{r.probe} ({r.stage} -> {r.other_stage}): {r.detail}" for r in failures)
        )
