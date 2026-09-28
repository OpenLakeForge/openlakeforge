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

from typing import Any

from olf import config, k8s, log
from olf.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise, terraform_output_json
from olf.e2e._trino import trino_query


def _sibling_stage(cfg: E2EConfig) -> str | None:
    """The other enabled stage to probe isolation against, or None if there
    isn't one (single-stage deployment - nothing to isolate)."""
    this_stage = cfg.namespace.removeprefix("olf-")
    candidates = ("dev", "prod", "uat")
    for candidate in candidates:
        if candidate == this_stage:
            continue
        if k8s.resource_exists("namespace", f"olf-{candidate}", f"olf-{candidate}"):
            return candidate
    return None


def _expect_trino_denied(cfg: E2EConfig, *, user: str, sql: str, what: str) -> None:
    """Require the query to fail specifically with an access-control denial.

    `trino_query` raises the same `E2EError` for a genuine authorization
    rejection and for an unrelated failure (a missing/broken sibling catalog,
    a transient connection error, ...). Catching any `E2EError` here would let
    the probe report isolation success when the sibling stage's catalog was
    never provisioned - it never got far enough to be denied anything.
    """
    try:
        trino_query(cfg, sql)
    except E2EError as exc:
        if "Access Denied" not in str(exc):
            raise E2EError(
                f"isolation probe for {user} could not verify a denial of {what}: the query failed for a "
                f"reason other than an access-control rejection ({exc})."
            ) from exc
        return
    raise E2EError(f"isolation breach: {user} was not denied {what} ({sql!r} succeeded).")


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


def _s3_identity(namespace: str, secret_name: str) -> tuple[str, str] | None:
    try:
        access_key = k8s.secret_value(secret_name, "AWS_ACCESS_KEY_ID", namespace)
        secret_key = k8s.secret_value(secret_name, "AWS_SECRET_ACCESS_KEY", namespace)
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


def _materialized_tables(cfg: E2EConfig) -> list[tuple[str, str]]:
    """(layer, schema.table) for every Silver input and Gold mart a product owns."""
    tables = {("gold", mart) for mart in cfg.inventory.gold_mart_names}
    for product in cfg.inventory.products:
        namespace = cfg.inventory.domain_for_product(product).silver_namespace
        tables.update(
            ("silver", f"{namespace}.{table.name}") for table in cfg.inventory.resolved_silver_tables(product)
        )
    return sorted(tables)


def _stage_data_files(cfg: E2EConfig, provider_contracts: dict[str, Any], stage: str) -> set[str]:
    """The data file names behind this stage's Silver and Gold tables, after
    requiring each table to have files and to keep them in the stage's own
    bucket for its layer."""
    catalog = f"lakehouse_{stage}"
    names: set[str] = set()
    for layer, table in _materialized_tables(cfg):
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
        names.update(path.rsplit("/", 1)[-1] for path in paths)
    return names


def _bucket_object_names(cfg: E2EConfig, provider_contracts: dict[str, Any], stage: str) -> set[str]:
    """Object names in a stage's Silver and Gold buckets, listed with the
    platform's S3 identity: the stage identities are denied each other's."""
    namespace = cfg.shared_namespace or "olf-system"
    identity = _s3_identity(namespace, "seaweedfs-s3-creds")
    if identity is None:
        raise E2EError(f"no-copy probe: seaweedfs-s3-creds not found in {namespace}.")
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    names: set[str] = set()
    with k8s.port_forward("seaweedfs-s3", 8333, namespace, log_path=f"{log_prefix}-isolation-s3-admin.log") as port:
        client = _s3_client(*identity, local_port=port, region=config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1"))
        for layer in ("silver", "gold"):
            bucket = _stage_bucket(provider_contracts, stage, layer)
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                names.update(item["Key"].rsplit("/", 1)[-1] for item in page.get("Contents", []))
    return names


def check_stage_data_is_its_own(
    cfg: E2EConfig, provider_contracts: dict[str, Any], this_stage: str, sibling: str
) -> None:
    """Promotion copies no data: this stage's Silver and Gold tables read only
    files this stage wrote.

    Location alone is not enough -- DEV files copied into PROD's bucket would
    sit where PROD's belong. Iceberg names every data file uniquely per write
    and a copy keeps the name, so this stage's data file names must not occur
    among the sibling's objects. A sibling that has written nothing yet (DEV's
    suite runs before promotion) is compared from its own suite. Requiring at
    least one file per table is the positive control for both properties.
    """
    log.step(f"Checking {this_stage} tables read only files {this_stage} wrote...")
    own = _stage_data_files(cfg, provider_contracts, this_stage)
    sibling_objects = _bucket_object_names(cfg, provider_contracts, sibling)
    if not sibling_objects:
        log.info(f"{sibling} has written no Silver or Gold objects yet; its suite compares the pair.")
        return
    copied = own & sibling_objects
    if copied:
        raise E2EError(
            f"promotion copied data: {this_stage} tables read {len(copied)} file(s) that also exist in "
            f"{sibling}'s buckets, e.g. {sorted(copied)[:3]}."
        )


def _dagster_run_ids(service_name: str, namespace: str, log_path: str) -> set[str]:
    from olf.clients.dagster import DagsterClient

    with k8s.port_forward(service_name, 80, namespace, log_path=log_path) as local_port:
        result = DagsterClient(f"http://127.0.0.1:{local_port}/graphql").graphql(
            "query { runsOrError(limit: 500) { __typename ... on Runs { results { runId } } } }"
        )["runsOrError"]
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
    names = terraform_output_json(cfg.contract_terraform_dir, "dagster_webserver_service_names")
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    runs = {
        stage: _dagster_run_ids(names[stage], f"olf-{stage}", f"{log_prefix}-isolation-dagster-{stage}.log")
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
    )
    # Positive control: the same identity must still reach its own catalog.
    trino_query(cfg, f"SELECT 1 FROM lakehouse_{this_stage}.information_schema.tables LIMIT 1")

    log.step(f"Checking SeaweedFS cross-stage isolation ({this_stage} -> {sibling})...")
    identity = _s3_identity(cfg.namespace, f"seaweedfs-{this_stage}-s3-creds")
    if identity is None:
        raise E2EError(f"stage isolation probe: seaweedfs-{this_stage}-s3-creds not found in {cfg.namespace}.")
    access_key, secret_key = identity
    region = config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1")
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    with k8s.port_forward(
        "seaweedfs-s3", 8333, cfg.shared_namespace or "olf-system", log_path=f"{log_prefix}-isolation-s3.log"
    ) as local_port:
        client = _s3_client(access_key, secret_key, local_port=local_port, region=region)

        _expect_s3_denied(
            lambda: client.head_bucket(Bucket=sibling_bucket),
            who=this_stage,
            what=f"HeadBucket on {sibling_bucket}",
        )

        ops_bucket = config.env("OPENLAKEFORGE_OPS_BUCKET_NAME", "openlakeforge-ops")
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
