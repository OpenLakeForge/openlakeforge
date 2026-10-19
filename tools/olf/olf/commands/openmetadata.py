"""OpenMetadata governance metadata helpers."""

from __future__ import annotations

import os

import typer

from olf import config
from olf.commands._shared import fail, log_step
from olf.openmetadata._ingestion import run_trino_ingestion
from olf.project import ProjectSpec

app = typer.Typer(help="OpenMetadata governance metadata helpers.")


@app.command("deploy-metadata")
def openmetadata_deploy_metadata(
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Seed metadata using the selected provider's Terraform contracts."""
    from olf.commands.runtime import provider_contract_environment

    with provider_contract_environment(
        provider=provider,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    ):
        deploy_openmetadata_metadata()


def deploy_openmetadata_metadata() -> None:
    """Seed OpenMetadata domains, data products, and medallion containers."""
    from olf import access, k8s
    from olf import openmetadata as om
    from olf.openmetadata._auth import automation_token
    from olf.openmetadata._sso import apply_sso

    project = ProjectSpec(root=config.project_root(), distribution_root=config.distribution_root())
    # OpenMetadata is deployed once per cluster, in the shared namespace -
    # `config.namespace()` is the selected stage's.
    namespace = config.shared_namespace()
    service = config.env("OPENMETADATA_SERVICE", "openmetadata")
    remote_port = int(config.env("OPENMETADATA_SERVICE_PORT", "8585"))

    log_step(f"Waiting for OpenMetadata deployment {service}...")
    k8s.wait_for_rollout(f"deployment/{service}", namespace)

    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    log_path = f"{log_prefix}-openmetadata-port-forward.log"
    # Before anything talks to the server: it restarts when SSO is applied.
    try:
        apply_sso(namespace, service=service, remote_port=remote_port, log_path=log_path)
    except (om.OpenMetadataError, k8s.KubectlError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    with access.service_url(
        "shared/governance_service",
        service=service,
        remote_port=remote_port,
        namespace=namespace,
        log_path=log_path,
        shared_namespace=namespace,
    ) as base_url:
        try:
            cfg = om.OpenMetadataConfig.from_environment(
                os.environ,
                base_url=base_url,
                admin_email=config.env("OPENMETADATA_ADMIN_EMAIL", "admin@open-metadata.org"),
                admin_password=config.env("OPENMETADATA_ADMIN_PASSWORD", "admin"),
                metadata_root=config.env("OPENMETADATA_METADATA_ROOT", str(project.code_root)),
                metadata_source_dir=os.environ.get("OPENMETADATA_METADATA_SOURCE_DIR", ""),
                allow_missing_assets=config.truthy(config.env("OPENMETADATA_ALLOW_MISSING_ASSETS", "false")),
                catalog_service=(
                    config.env("OPENMETADATA_CATALOG_SERVICE") or config.env("OPENLAKEFORGE_CATALOG_PROVIDER")
                ),
                catalog_database=(
                    config.env("OPENMETADATA_CATALOG_DATABASE") or config.env("OPENLAKEFORGE_CATALOG_NAME")
                ),
            )
            # Before the seeding, which fails on tables OpenMetadata has not
            # crawled yet. Once per artifacts deploy, no CronJob: the pipeline
            # it replaces was deployed paused and never scheduled.
            log_step("Crawling Trino into OpenMetadata...")
            run_trino_ingestion(os.environ, namespace=namespace, service=cfg.catalog_service)
            client = om.OpenMetadataClient(cfg.base_url, token=automation_token(namespace))
            om.OpenMetadataDeployer(cfg, client).deploy()
        except (om.OpenMetadataError, k8s.KubectlError) as exc:
            raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("Deployed OpenMetadata governance metadata.")
