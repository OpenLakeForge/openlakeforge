"""Ops-bucket artifact assertions."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from typing import Any

import boto3
from botocore.config import Config
from openlakeforge_core import LakehouseInventory

from olf.common import log
from olf.infra import k8s
from olf.infra.auth import aws_session
from olf.infra.e2e._shell import (
    E2EConfig,
    E2EError,
    aws_stack_region,
    cfg_environ,
    kubectl,
    load_provider_contracts_or_raise,
)

# Dagster's own compute-log archive path, relative to this stage's own
# activations/<stage> prefix (modules/orchestration/dagster/release.tf
# derives the compute-log-manager prefix from the stage-scoped
# log_base_uri, matching the SeaweedFS grant scoped to that same prefix).
ARTIFACT_PREFIXES = ("logs/dagster/compute/",)
# The collector holds a stage's records for its `upload_timeout` (5m, modules/observability/log-collector) before the
# first object exists, and nothing triggers an upload on local the way the CronJob Job did elsewhere; 7 minutes
# covers that window, the 1 s Flush and the upload itself.
FIRST_V2_OBJECT_TIMEOUT_SECONDS = 420.0
FIRST_V2_OBJECT_POLL_SECONDS = 15.0


def check_ops_artifacts(cfg: E2EConfig) -> None:
    log.step("Checking ops artifact bucket contents...")
    # The local stack has no polling CronJob: the node collector archives pod logs (ADR 0022).
    if cfg.env != "local":
        trigger_log_archive_job(cfg)
    with ops_bucket_client(cfg) as (client, bucket):
        if cfg.env == "local":
            wait_for_first_object(client, bucket, pod_log_prefix(cfg.namespace, "v2"))
        assert_ops_artifacts(client, bucket, cfg.namespace, cfg.inventory, pod_log_layout=_pod_log_layout(cfg))


def pod_log_prefix(namespace: str, layout: str) -> str:
    """Where a stage's pod logs are archived, in the v1 (`k8s`) or the collector's v2 layout."""
    return f"activations/{namespace.removeprefix('olf-')}/logs/{layout}/namespace={namespace}/"


def wait_for_first_object(
    client: Any,
    bucket: str,
    prefix: str,
    *,
    timeout: float = FIRST_V2_OBJECT_TIMEOUT_SECONDS,
    poll: float = FIRST_V2_OBJECT_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return once an object exists under `prefix`; an empty prefix past `timeout` is an error, never a pass."""
    deadline = clock() + timeout
    while not client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("Contents"):
        if clock() >= deadline:
            raise E2EError(f"no object under s3://{bucket}/{prefix} after {timeout:.0f}s")
        sleep(poll)


def _pod_log_layout(cfg: E2EConfig) -> str:
    # The node collector writes v2 on local; the polling CronJob writes v1 (`k8s`) elsewhere.
    return "v2" if cfg.env == "local" else "k8s"


@contextlib.contextmanager
def ops_bucket_client(cfg: E2EConfig) -> Iterator[tuple[Any, str]]:
    """An S3 client for the shared ops bucket, through the provider's own access path, and the bucket's name."""
    provider_contracts = load_provider_contracts_or_raise(cfg)
    bucket = provider_contracts["shared"]["ops_storage"]["bucket_name"]
    if cfg.env == "aws":
        yield aws_session(cfg_environ(cfg), region=aws_stack_region(cfg)).client("s3"), bucket
        return

    assert cfg.seaweedfs_local_port is not None
    access_key_id = k8s.secret_value(
        "seaweedfs-s3-creds",
        "AWS_ACCESS_KEY_ID",
        cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg.environ,
    )
    secret_access_key = k8s.secret_value(
        "seaweedfs-s3-creds",
        "AWS_SECRET_ACCESS_KEY",
        cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg.environ,
    )
    log_path = f"/tmp/openlakeforge-{cfg.env}-seaweedfs-s3-port-forward.log"
    with k8s.port_forward(
        "seaweedfs-s3",
        8333,
        cfg.platform_namespace,
        local_port=cfg.seaweedfs_local_port,
        log_path=log_path,
        kube_context=cfg.kube_context,
        environ=cfg.environ,
    ):
        endpoint = f"http://127.0.0.1:{cfg.seaweedfs_local_port}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),
        )
        wait_for_bucket(client, bucket, endpoint)
        yield client, bucket


def trigger_log_archive_job(cfg: E2EConfig) -> None:
    archive_job = f"openlakeforge-k8s-log-archive-e2e-{int(time.time())}"
    kubectl(
        cfg,
        [
            "create",
            "job",
            "--from=cronjob/openlakeforge-k8s-log-archive",
            archive_job,
            "-n",
            cfg.namespace,
        ],
    )
    kubectl(
        cfg,
        [
            "wait",
            "--for=condition=complete",
            f"job/{archive_job}",
            "-n",
            cfg.namespace,
            "--timeout=300s",
        ],
    )


def assert_ops_artifacts(
    client: Any,
    bucket: str,
    namespace: str,
    inventory: LakehouseInventory,
    *,
    pod_log_layout: str = "k8s",
) -> None:
    # A product-less domain (see olf domain new) has no product job selecting
    # its Silver tables, so Floe never runs for it and it never writes a
    # runtime report -- only domains with at least one product produce one.
    floe_prefixes = tuple(
        domain.artifact_prefixes.floe_report_prefix for domain in inventory.domains if domain.products
    )
    # run-artifacts, Dagster compute logs, and k8s logs are all written
    # beneath this stage's own activations/<stage> prefix (see
    # OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI / OPENLAKEFORGE_LOG_BASE_URI).
    # Floe reports follow OPENLAKEFORGE_ARTIFACT_BASE_URI (openlakeforge_runtime/floe_profile.py):
    # bucket-root under `olf infra deploy`, whose v2 contract exports that base URI,
    # but stage-scoped once `olf infra project deploy` activates a revision.
    activation_prefix = f"activations/{namespace.removeprefix('olf-')}"
    for prefix in floe_prefixes:
        require_any_s3_prefix(client, bucket, (prefix, f"{activation_prefix}/{prefix}"))
    dbt_prefixes = tuple(f"{activation_prefix}/{product.dbt_artifact_prefix}" for product in inventory.products)
    scoped_artifact_prefixes = tuple(f"{activation_prefix}/{prefix}" for prefix in ARTIFACT_PREFIXES)
    for prefix in (
        *dbt_prefixes,
        *scoped_artifact_prefixes,
        pod_log_prefix(namespace, pod_log_layout),
    ):
        require_s3_prefix(client, bucket, prefix)


def wait_for_bucket(client: Any, bucket: str, endpoint: str, *, attempts: int = 60, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            client.head_bucket(Bucket=bucket)
            return
        except Exception as exc:
            if attempt == attempts:
                raise E2EError(f"bucket '{bucket}' did not become available through {endpoint}.") from exc
            time.sleep(delay)


def require_s3_prefix(client: Any, bucket: str, prefix: str) -> None:
    require_any_s3_prefix(client, bucket, (prefix,))


def require_any_s3_prefix(client: Any, bucket: str, prefixes: tuple[str, ...]) -> None:
    for prefix in prefixes:
        if client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("Contents"):
            return
    raise E2EError("expected objects under " + " or ".join(f"s3://{bucket}/{prefix}" for prefix in prefixes))
