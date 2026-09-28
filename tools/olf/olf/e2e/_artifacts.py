"""Ops-bucket and Floe-manifest artifact assertions."""

from __future__ import annotations

import time
from typing import Any

import boto3
from botocore.config import Config
from openlakeforge_domain import LakehouseInventory

from olf import k8s, log, revision
from olf.e2e._dagster import expected_repository_location_names, expected_user_code_pods
from olf.e2e._shell import E2EConfig, E2EError, aws_stack_region, kubectl, load_provider_contracts_or_raise

# Dagster's own compute-log archive path, relative to this stage's own
# activations/<stage> prefix (modules/orchestration/dagster/release.tf
# derives the compute-log-manager prefix from the stage-scoped
# log_base_uri, matching the SeaweedFS grant scoped to that same prefix).
ARTIFACT_PREFIXES = ("logs/dagster/compute/",)


def check_ops_artifacts(cfg: E2EConfig) -> None:
    log.step("Checking ops artifact bucket contents...")
    trigger_log_archive_job(cfg)
    deployed_revision = deployed_floe_manifest_revision(cfg)
    provider_contracts = load_provider_contracts_or_raise(cfg)
    bucket = provider_contracts["shared"]["ops_storage"]["bucket_name"]
    if cfg.env == "aws":
        client = boto3.client("s3", region_name=aws_stack_region(cfg))
        assert_ops_artifacts(client, bucket, cfg.namespace, cfg.inventory, deployed_revision)
        return

    assert cfg.seaweedfs_local_port is not None
    access_key_id = k8s.secret_value(
        "seaweedfs-s3-creds",
        "AWS_ACCESS_KEY_ID",
        cfg.platform_namespace,
        kube_context=cfg.kube_context,
    )
    secret_access_key = k8s.secret_value(
        "seaweedfs-s3-creds",
        "AWS_SECRET_ACCESS_KEY",
        cfg.platform_namespace,
        kube_context=cfg.kube_context,
    )
    log_path = f"/tmp/openlakeforge-{cfg.env}-seaweedfs-s3-port-forward.log"
    with k8s.port_forward(
        "seaweedfs-s3",
        8333,
        cfg.platform_namespace,
        local_port=cfg.seaweedfs_local_port,
        log_path=log_path,
        kube_context=cfg.kube_context,
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
        assert_ops_artifacts(client, bucket, cfg.namespace, cfg.inventory, deployed_revision)


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


def deployed_floe_manifest_revision(cfg: E2EConfig) -> str:
    """Read the Floe revision each running Dagster code pod resolves at runtime."""
    location_names = expected_repository_location_names(cfg)
    pods = expected_user_code_pods(cfg, location_names)
    if not pods:
        raise E2EError("no running Dagster user-code pods were found to identify the deployed Floe revision.")

    revisions: dict[str, str] = {}
    for pod in pods:
        environ = dict(
            line.split("=", 1)
            for line in kubectl(cfg, ["exec", "-n", cfg.namespace, pod, "--", "env"], capture=True).splitlines()
            if "=" in line
        )
        # The runtime's own precedence (libs/floe_revision.py): an activated
        # stage's revision over the image-baked one, which `olf project image`
        # leaves as "manual".
        value = next(
            (
                environ[name].strip()
                for name in ("OPENLAKEFORGE_FLOE_MANIFEST_REVISION", "OPENLAKEFORGE_FLOE_MANIFEST_REVISION_BUILT")
                if environ.get(name, "").strip() not in ("", "manual")
            ),
            environ.get("OPENLAKEFORGE_FLOE_MANIFEST_REVISION_BUILT", "").strip(),
        )
        if not value:
            raise E2EError(f"Dagster user-code pod {pod} has no Floe manifest revision marker.")
        revisions[pod] = value

    unique_revisions = set(revisions.values())
    if len(unique_revisions) != 1:
        details = ", ".join(f"{pod}={value}" for pod, value in sorted(revisions.items()))
        raise E2EError(f"Dagster user-code pods disagree on the built Floe manifest revision: {details}.")
    return unique_revisions.pop()


def assert_ops_artifacts(
    client: Any,
    bucket: str,
    namespace: str,
    inventory: LakehouseInventory,
    deployed_revision: str,
) -> None:
    if deployed_revision == "manual":
        assert_legacy_floe_manifests(client, bucket, inventory)
    else:
        assert_immutable_floe_manifests(client, bucket, inventory, deployed_revision)
    # A product-less domain (see olf domain new) has no product job selecting
    # its Silver tables, so Floe never runs for it and it never writes a
    # runtime report -- only domains with at least one product produce one.
    floe_prefixes = tuple(
        domain.artifact_prefixes.floe_report_prefix for domain in inventory.domains if domain.products
    )
    # run-artifacts, Dagster compute logs, and k8s logs are all written
    # beneath this stage's own activations/<stage> prefix (see
    # OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI / OPENLAKEFORGE_LOG_BASE_URI).
    # Floe reports are bucket-root under `olf deploy`, whose v2 contract
    # exports that base URI, but stage-scoped once `olf project deploy`
    # activates a revision with the stage's own OPENLAKEFORGE_FLOE_REPORT_BASE_URI.
    activation_prefix = f"activations/{namespace.removeprefix('olf-')}"
    for prefix in floe_prefixes:
        require_any_s3_prefix(client, bucket, (prefix, f"{activation_prefix}/{prefix}"))
    dbt_prefixes = tuple(f"{activation_prefix}/{product.dbt_artifact_prefix}" for product in inventory.products)
    scoped_artifact_prefixes = tuple(f"{activation_prefix}/{prefix}" for prefix in ARTIFACT_PREFIXES)
    for prefix in (
        *dbt_prefixes,
        *scoped_artifact_prefixes,
        f"{activation_prefix}/logs/k8s/namespace={namespace}/",
    ):
        require_s3_prefix(client, bucket, prefix)


def assert_immutable_floe_manifests(
    client: Any, bucket: str, inventory: LakehouseInventory, deployed_revision: str
) -> None:
    try:
        manifest = revision.verify(client, bucket, deployed_revision)
    except revision.RevisionError as exc:
        raise E2EError(f"failed to verify deployed immutable Floe revision {deployed_revision}: {exc}") from exc
    expected_manifest_keys = set(inventory.manifest_keys)
    missing = expected_manifest_keys.difference(manifest.entries)
    if missing:
        raise E2EError(
            f"deployed immutable Floe revision {deployed_revision} does not contain every "
            f"descriptor-discovered domain manifest: {', '.join(sorted(missing))}."
        )


def assert_legacy_floe_manifests(client: Any, bucket: str, inventory: LakehouseInventory) -> None:
    """Verify mutable manifests for a supplied local project-code image."""
    for key in inventory.manifest_keys:
        try:
            client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise E2EError(f"missing legacy Floe manifest s3://{bucket}/{key}: {exc}") from exc


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
