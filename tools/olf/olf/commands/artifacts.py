"""Object-storage artifact helpers."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from olf import config
from olf import layers as layers_module
from olf.commands._shared import fail
from olf.commands.openmetadata import deploy_openmetadata_metadata
from olf.commands.superset import deploy_superset_reports

app = typer.Typer(help="Object-storage artifact helpers.")


@app.command("deploy-optional-layers")
def artifacts_deploy_optional_layers() -> None:
    """Deploy artifacts for enabled optional platform layers."""
    layers_module.deploy_enabled_artifacts(
        os.environ,
        deploy_reports=deploy_superset_reports,
        deploy_metadata=deploy_openmetadata_metadata,
        report=lambda message: typer.echo(f"==> {message}"),
    )


@app.command("upload-manifests")
def artifacts_upload_manifests(
    via: str = typer.Option(
        "port-forward",
        "--via",
        help="'port-forward' for in-cluster S3-compatible storage, 'direct' for cloud S3.",
    ),
    manifest_root: str = typer.Option(
        "",
        "--manifest-root",
        help="Rendered-manifest directory for --via direct (default: .tmp/floe-runtime/aws/manifests).",
    ),
    runtime_root: str = typer.Option(
        "",
        "--runtime-root",
        help="Rendered Floe runtime artifact root containing configs/, profiles/, and manifests/.",
    ),
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Publish artifacts using the selected provider's Terraform contracts."""
    from olf.commands.runtime import provider_contract_environment

    with provider_contract_environment(
        provider=provider,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    ):
        upload_manifests(via=via, manifest_root=manifest_root, runtime_root=runtime_root)


def upload_manifests(
    *,
    via: str = "port-forward",
    manifest_root: str = "",
    runtime_root: str = "",
) -> None:
    """Publish domain Floe runtime artifacts to the operational artifact bucket."""
    from olf import s3

    project = config.project_spec()
    # This upload writes outside any stage's own activations/<stage> prefix
    # (see olf.artifact_store.artifact_storage_client), so it needs the
    # shared admin identity, read from the shared namespace, not
    # config.namespace()'s stage-selected one.
    namespace = config.shared_namespace()
    bucket = config.env("OPENLAKEFORGE_OPS_BUCKET_NAME") or config.env("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME")
    if not bucket:
        raise typer.Exit(code=fail("no ops/artifact bucket resolved from the contract environment."))

    if via == "direct":
        if runtime_root:
            uploads = s3.discover_runtime_artifacts(Path(runtime_root))
        else:
            root = Path(manifest_root) if manifest_root else project.root / ".tmp/floe-runtime/aws/manifests"
            uploads = s3.discover_runtime_manifests(root)
        if not uploads:
            searched = runtime_root or manifest_root or str(project.root / ".tmp/floe-runtime/aws/manifests")
            raise typer.Exit(code=fail(f"no rendered Floe artifacts found under {searched}."))
        s3.upload_direct(bucket, uploads, region=config.env("OPENLAKEFORGE_STORAGE_REGION"))
    elif via == "port-forward":
        if runtime_root:
            uploads = s3.discover_runtime_artifacts(Path(runtime_root))
        else:
            uploads = s3.discover_tracked_manifests(project.root)
        if not uploads:
            raise typer.Exit(
                code=fail("no generated domain Floe artifacts found. Run 'olf floe generate-manifests' first.")
            )
        secret_name = config.env("OPENLAKEFORGE_OPS_STORAGE_CREDENTIALS_SECRET_NAME")
        service = config.env("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAME", "seaweedfs-s3")
        remote_port = int(config.env("OPENLAKEFORGE_STORAGE_S3_SERVICE_PORT", "8333"))
        service_namespace = config.env("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAMESPACE") or namespace
        from olf import k8s

        s3.upload_via_port_forward(
            bucket,
            uploads,
            service=service,
            remote_port=remote_port,
            namespace=service_namespace,
            access_key_id=k8s.secret_value(
                secret_name, config.env("OPENLAKEFORGE_OPS_STORAGE_ACCESS_KEY_ID_KEY", "AWS_ACCESS_KEY_ID"), namespace
            ),
            secret_access_key=k8s.secret_value(
                secret_name,
                config.env("OPENLAKEFORGE_OPS_STORAGE_SECRET_ACCESS_KEY_KEY", "AWS_SECRET_ACCESS_KEY"),
                namespace,
            ),
            region=config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1"),
        )
    else:
        raise typer.Exit(code=fail(f"unknown --via mode: {via!r} (expected 'port-forward' or 'direct')."))
