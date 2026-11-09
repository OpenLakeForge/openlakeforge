"""Cross-stage isolation probes.

Proves the acceptance criterion #114 exists for: "DEV runtime configuration
cannot read/write PROD, and vice versa." Every probe is paired with a
positive control against the caller's own stage, so the suite cannot pass
merely because the cluster is broken rather than because isolation holds.

Local-only today: local is the only stage-aware root with more than one
stage actually provisioned in this repository's verification profile. A
deployment with only one enabled stage has nothing to isolate against, so
this module skips cleanly rather than failing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openlakeforge_services.base import ServiceClientError

from olf.common import config, log
from olf.infra import k8s, trino
from olf.infra.e2e._dagster import DAGSTER_WEBSERVER_SERVICE_NAME
from olf.infra.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise
from olf.infra.e2e._trino import PROBE_SCHEMA, list_tables, processing_credential, trino_query


def _sibling_stage(cfg: E2EConfig) -> str | None:
    """The other enabled stage to probe isolation against, or None if there
    isn't one (single-stage deployment - nothing to isolate)."""
    this_stage = cfg.namespace.removeprefix("olf-")
    candidates = ("dev", "prod", "uat")
    for candidate in candidates:
        if candidate == this_stage:
            continue
        if k8s.resource_exists("namespace", f"olf-{candidate}", f"olf-{candidate}", environ=cfg.environ):
            return candidate
    return None


def _expect_trino_denied(cfg: E2EConfig, *, user: str, sql: str, what: str, sibling: str) -> None:
    """Require the query to fail because this stage's Trino cannot see the sibling's catalog.

    Each stage's coordinator holds only its own catalogs (ADR 0019 section 9),
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


def _bronze_bucket_for_stage(provider_contracts: dict[str, Any], stage: str) -> str:
    try:
        bucket = provider_contracts["stages"][stage]["storage"]["bronze"]["bucket_name"]
    except (KeyError, TypeError) as exc:
        raise E2EError(
            f"provider_contracts.stages.{stage}.storage.bronze.bucket_name is required for isolation."
        ) from exc
    if not isinstance(bucket, str) or not bucket:
        raise E2EError(f"provider_contracts.stages.{stage}.storage.bronze.bucket_name must be a non-empty string.")
    return bucket


def _stage_bucket(provider_contracts: dict[str, Any], stage: str, layer: str) -> str:
    try:
        bucket = provider_contracts["stages"][stage]["storage"][layer]["bucket_name"]
    except (KeyError, TypeError) as exc:
        raise E2EError(f"provider_contracts.stages.{stage}.storage.{layer}.bucket_name is required.") from exc
    return str(bucket)


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
) -> None:
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
        return
    copied = _bucket_data_files(cfg, provider_contracts, this_stage) & sibling_files
    if copied:
        raise E2EError(
            f"promotion copied data: {this_stage}'s buckets hold {len(copied)} data file(s) that also exist "
            f"in {sibling}'s, e.g. {sorted(copied)[:3]}."
        )


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


def check_dagster_state_isolation(cfg: E2EConfig, this_stage: str, sibling: str) -> None:
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
        return
    shared = runs[this_stage] & runs[sibling]
    if shared:
        raise E2EError(f"isolation breach: {this_stage} and {sibling} Dagster share runs {sorted(shared)[:5]}.")


def check_stage_isolation(cfg: E2EConfig) -> None:
    """Prove this stage's runtime identity cannot read/write the sibling
    stage's Trino catalog, S3 buckets, or ops-bucket activation prefix -
    and, as a positive control, that it *can* reach its own."""
    if cfg.env != "local":
        log.info(f"Skipping stage isolation probe: not yet wired for {cfg.env}.")
        return
    sibling = _sibling_stage(cfg)
    if sibling is None:
        log.info("Skipping stage isolation probe: only one stage is enabled.")
        return

    this_stage = cfg.namespace.removeprefix("olf-")
    provider_contracts = load_provider_contracts_or_raise(cfg)
    this_bucket = _bronze_bucket_for_stage(provider_contracts, this_stage)
    sibling_bucket = _bronze_bucket_for_stage(provider_contracts, sibling)
    runtime_user = f"{cfg.namespace}-runtime"
    log.step(f"Checking Trino cross-stage isolation ({this_stage} -> {sibling})...")
    _expect_trino_denied(
        cfg,
        user=runtime_user,
        sql=f"SELECT 1 FROM lakehouse_{sibling}.information_schema.tables LIMIT 1",
        what=f"read access to lakehouse_{sibling}",
        sibling=sibling,
    )
    # Positive control: the same identity must still reach its own catalog.
    trino_query(cfg, f"SELECT 1 FROM lakehouse_{this_stage}.information_schema.tables LIMIT 1")
    check_keyless_trino(cfg, provider_contracts, this_stage, sibling)

    log.step(f"Checking SeaweedFS cross-stage isolation ({this_stage} -> {sibling})...")
    identity = _s3_identity(cfg.namespace, f"seaweedfs-{this_stage}-s3-creds", environ=cfg.environ)
    if identity is None:
        raise E2EError(f"stage isolation probe: seaweedfs-{this_stage}-s3-creds not found in {cfg.namespace}.")
    access_key, secret_key = identity
    region = config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1", environ=cfg.environ)
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge", environ=cfg.environ)
    with k8s.port_forward(
        "seaweedfs-s3",
        8333,
        cfg.shared_namespace or "olf-system",
        log_path=f"{log_prefix}-isolation-s3.log",
        environ=cfg.environ,
    ) as local_port:
        client = _s3_client(access_key, secret_key, local_port=local_port, region=region)

        _expect_s3_denied(
            lambda: client.head_bucket(Bucket=sibling_bucket),
            who=this_stage,
            what=f"HeadBucket on {sibling_bucket}",
        )

        ops_bucket = config.env("OPENLAKEFORGE_OPS_BUCKET_NAME", "openlakeforge-ops", environ=cfg.environ)
        _expect_s3_denied(
            lambda: client.list_objects_v2(Bucket=ops_bucket, Prefix=f"activations/{sibling}/", MaxKeys=1),
            who=this_stage,
            what=f"listing activations/{sibling}/ in the shared ops bucket",
        )

        # Positive controls: the same identity must still reach its own
        # bucket and its own ops-bucket activation prefix.
        client.head_bucket(Bucket=this_bucket)
        client.list_objects_v2(Bucket=ops_bucket, Prefix=f"activations/{this_stage}/", MaxKeys=1)

    check_stage_data_is_its_own(cfg, provider_contracts, this_stage, sibling)
    check_dagster_state_isolation(cfg, this_stage, sibling)
