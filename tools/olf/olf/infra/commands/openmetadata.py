"""OpenMetadata governance metadata helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

import typer

from olf.common import config, log
from olf.common.log import fail
from olf.common.profile import DeploymentProfileError
from olf.common.project import ProjectSpec
from olf.infra.openmetadata._ingestion import run_trino_ingestion

if TYPE_CHECKING:
    from olf.infra.access import Route

app = typer.Typer(help="OpenMetadata governance metadata helpers.")


@dataclass(frozen=True)
class _GovernanceService:
    namespace: str
    service: str
    remote_port: int
    log_path: str

    @contextmanager
    def url(self, environ: Mapping[str, str] | None = None) -> Iterator[Route]:
        from olf.infra import access

        with access.service_route(
            "shared/governance_service",
            service=self.service,
            remote_port=self.remote_port,
            namespace=self.namespace,
            log_path=self.log_path,
            shared_namespace=self.namespace,
            environ=environ,
        ) as route:
            yield route


def _governance_service(environ: Mapping[str, str] | None = None) -> _GovernanceService:
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge", environ=environ)
    return _GovernanceService(
        # OpenMetadata is deployed once per cluster, in the shared namespace -
        # `config.namespace()` is the selected stage's.
        namespace=config.shared_namespace(environ=environ),
        service=config.env("OPENMETADATA_SERVICE", "openmetadata", environ=environ),
        remote_port=int(config.env("OPENMETADATA_SERVICE_PORT", "8585", environ=environ)),
        log_path=f"{log_prefix}-openmetadata-port-forward.log",
    )


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
    from olf.infra.commands.runtime import provider_contract_environment

    with provider_contract_environment(
        provider=provider,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    ) as contract:
        deploy_openmetadata_metadata(environ=contract)


def deploy_openmetadata_metadata(
    *, allow_missing_assets: bool | None = None, environ: Mapping[str, str] | None = None
) -> None:
    """Seed OpenMetadata domains, data products, and medallion containers.

    ``allow_missing_assets`` overrides ``OPENMETADATA_ALLOW_MISSING_ASSETS`` when not None.
    """
    from olf.infra import k8s
    from olf.infra import openmetadata as om
    from olf.infra.openmetadata._auth import admin_password, automation_token
    from olf.infra.openmetadata._role_sync import apply_role_sync
    from olf.infra.openmetadata._sso import apply_sso

    source = os.environ if environ is None else environ
    project = ProjectSpec(
        root=config.project_root(environ=source), distribution_root=config.distribution_root(environ=source)
    )
    governance = _governance_service(source)
    namespace = governance.namespace

    log.step(f"Waiting for OpenMetadata deployment {governance.service}...")
    k8s.wait_for_rollout(f"deployment/{governance.service}", namespace, environ=source)

    # Before anything talks to the server: it restarts when SSO is applied.
    try:
        apply_sso(
            namespace,
            service=governance.service,
            remote_port=governance.remote_port,
            log_path=governance.log_path,
            environ=source,
        )
    except (om.OpenMetadataError, k8s.KubectlError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    try:
        log.step(
            apply_role_sync(namespace, om_url=f"http://{governance.service}:{governance.remote_port}", environ=source)
        )
    except (k8s.KubectlError, DeploymentProfileError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    with governance.url(source) as route:
        try:
            token = automation_token(namespace, environ=source)
            override = config.env("OPENMETADATA_ADMIN_PASSWORD", environ=source)
            password = override if token else admin_password(namespace, override, environ=source)
            cfg = om.OpenMetadataConfig.from_environment(
                source,
                base_url=route.url,
                admin_email=config.env("OPENMETADATA_ADMIN_EMAIL", "admin@open-metadata.org", environ=source),
                admin_password=password,
                metadata_root=config.env("OPENMETADATA_METADATA_ROOT", str(project.code_root), environ=source),
                metadata_source_dir=source.get("OPENMETADATA_METADATA_SOURCE_DIR", ""),
                allow_missing_assets=(
                    config.truthy(config.env("OPENMETADATA_ALLOW_MISSING_ASSETS", "false", environ=source))
                    if allow_missing_assets is None
                    else allow_missing_assets
                ),
                catalog_service=(
                    config.env("OPENMETADATA_CATALOG_SERVICE", environ=source)
                    or config.env("OPENLAKEFORGE_CATALOG_PROVIDER", environ=source)
                ),
                catalog_database=(
                    config.env("OPENMETADATA_CATALOG_DATABASE", environ=source)
                    or config.env("OPENLAKEFORGE_CATALOG_NAME", environ=source)
                ),
            )
            # Before the seeding, which fails on tables OpenMetadata has not
            # crawled yet. Once per artifacts deploy, no CronJob: the pipeline
            # it replaces was deployed paused and never scheduled.
            log.step("Crawling Trino into OpenMetadata...")
            run_trino_ingestion(source, namespace=namespace, service=cfg.catalog_service)
            client = route.client(om.OpenMetadataClient, token=token)
            om.OpenMetadataDeployer(cfg, client).deploy()
        except (om.OpenMetadataError, k8s.KubectlError) as exc:
            raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("Deployed OpenMetadata governance metadata.")


@app.command("bootstrap")
def openmetadata_bootstrap(
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    om_url: str = typer.Option("", "--om-url", help="OpenMetadata URL; default is a port-forward to the service."),
    sso: bool = typer.Option(False, "--sso", help="Also create the admin automation bot SSO needs."),
    catalog_service: str = typer.Option(
        "", "--catalog-service", help="Also seed OpenLineage, this lakehouse database service and its databases."
    ),
    catalog_display: str = typer.Option("", "--catalog-display", help="Display name of the catalog service."),
    trino_namespace: str = typer.Option("", "--trino-namespace", help="Trino's `trino://host:port` namespace."),
    trino_tls_namespace: str = typer.Option("", "--trino-tls-namespace", help="Trino's HTTPS namespace, if any."),
    pipeline_service: str = typer.Option("", "--pipeline-service", help="OpenLineage's default pipeline service."),
    database: Annotated[
        list[str] | None, typer.Option("--database", help="A governed stage's catalog database; repeatable.")
    ] = None,
    schema: Annotated[
        list[str] | None, typer.Option("--schema", help="A schema every stage database holds; repeatable.")
    ] = None,
) -> None:
    """Create the admin password, bots and, with --catalog-service, the catalog entities. Idempotent."""
    from olf.infra import k8s
    from olf.infra import openmetadata as om
    from olf.infra.access import Route
    from olf.infra.commands.runtime import provider_contract_environment
    from olf.infra.openmetadata._bootstrap import bootstrap_auth
    from olf.infra.openmetadata._catalog_seed import CatalogSeed, bootstrap_catalog

    seed = (
        CatalogSeed(
            service=catalog_service,
            display_name=catalog_display or catalog_service,
            trino_namespace=trino_namespace,
            trino_tls_namespace=trino_tls_namespace,
            pipeline_service=pipeline_service,
            databases=tuple(database or ()),
            schemas=tuple(schema or ()),
        )
        if catalog_service
        else None
    )
    if seed and not (trino_namespace and pipeline_service):
        raise typer.Exit(code=fail("--catalog-service needs --trino-namespace and --pipeline-service."))

    def run(route: Route, namespace: str) -> None:
        client = bootstrap_auth(route, namespace, email=email, sso=sso, environ=contract)
        if seed:
            bootstrap_catalog(client, seed)

    with provider_contract_environment(
        provider=provider,
        profile="",
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    ) as contract:
        email = config.env("OPENMETADATA_ADMIN_EMAIL", "admin@open-metadata.org", environ=contract)
        governance = _governance_service(contract)
        try:
            if om_url:
                run(Route(om_url), governance.namespace)
            else:
                with governance.url(contract) as route:
                    run(route, governance.namespace)
        except (om.OpenMetadataError, k8s.KubectlError) as exc:
            raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("OpenMetadata bootstrap complete.")


@app.command("list-domains")
def openmetadata_list_domains(
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the domains as JSON."),
) -> None:
    """List the domains OpenMetadata exposes, sorted by name. Read-only."""
    from olf.infra.commands.runtime import provider_contract_environment

    with provider_contract_environment(
        provider=provider,
        profile="",
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    ) as contract:
        domains = sorted(list_openmetadata_domains(environ=contract))
    if json_output:
        typer.echo(json.dumps([{"name": name, "displayName": display} for name, display in domains], indent=2))
    elif not domains:
        typer.echo("OpenMetadata has no domains.")
    else:
        width = max(len("NAME"), *(len(name) for name, _ in domains))
        typer.echo(f"{'NAME':<{width}}  DISPLAY NAME")
        for name, display in domains:
            typer.echo(f"{name:<{width}}  {display}")


def list_openmetadata_domains(environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Read every domain's name and display name from the deployed OpenMetadata."""
    from openlakeforge_services.base import ServiceClientError
    from openlakeforge_services.openmetadata import OpenMetadataClient

    from olf.common import layers
    from olf.infra import k8s
    from olf.infra.openmetadata._auth import AUTOMATION_BOT, automation_token

    if not layers.enabled(os.environ if environ is None else environ, "governance"):
        raise typer.Exit(
            code=fail(
                "OpenMetadata is not deployed: the governance layer is disabled. "
                "Set `preset: full` in openlakeforge.yaml and run `olf infra deploy`."
            )
        )
    governance = _governance_service(environ)
    namespace = governance.namespace
    try:
        with governance.url(environ) as route:
            # Read after the port-forward is up, so an unreachable cluster is
            # reported as unreachable rather than as a missing Secret.
            token = automation_token(namespace, environ=environ)
            if not token:
                raise typer.Exit(
                    code=fail(
                        f"No OpenMetadata automation token: Secret {AUTOMATION_BOT} is missing from namespace "
                        f"{namespace}. The governance bootstrap Job writes it; rerun `olf infra deploy` to recreate it."
                    )
                )
            return route.client(OpenMetadataClient, token=token).list_domains()
    except k8s.KubectlError as exc:
        raise typer.Exit(
            code=fail(f"OpenMetadata is not reachable in namespace {namespace}; is the platform deployed? {exc}")
        ) from exc
    except ServiceClientError as exc:
        raise typer.Exit(code=fail(f"OpenMetadata did not list its domains: {exc}")) from exc
