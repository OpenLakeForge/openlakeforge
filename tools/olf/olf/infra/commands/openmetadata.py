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
from olf.infra.commands._shared import PROVIDER_HELP
from olf.infra.openmetadata._ingestion import run_trino_ingestion

if TYPE_CHECKING:
    from olf.common.provider_contracts import ProviderContracts
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
    provider: str = typer.Option("", "--provider", help=PROVIDER_HELP),
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
            admin_email=config.env("OPENMETADATA_ADMIN_EMAIL", "admin@open-metadata.org", environ=source),
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
            # crawled yet. One crawl now, plus the hourly CronJob that keeps
            # the catalog current between deploys.
            log.step("Crawling Trino into OpenMetadata...")
            run_trino_ingestion(source, namespace=namespace, service=cfg.catalog_service)
            client = route.client(om.OpenMetadataClient, token=token)
            om.OpenMetadataDeployer(cfg, client).deploy()
        except (om.OpenMetadataError, k8s.KubectlError) as exc:
            raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("Deployed OpenMetadata governance metadata.")


@app.command("quality-sync")
def openmetadata_quality_sync(
    provider: str = typer.Option("", "--provider", help=PROVIDER_HELP),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    stage: str = typer.Option("", "--stage", help="Stage to publish; defaults to the project's default stage."),
    run_id: str = typer.Option("", "--run", help="Replay this Dagster run, whatever its sync state."),
    limit: int = typer.Option(25, "--limit", help="Most runs to work on in this invocation (1-200)."),
    retry_failed: bool = typer.Option(
        False, "--retry-failed", help="Give results the reconciler stopped retrying a fresh set of attempts."
    ),
    warn_status: str = typer.Option(
        "Aborted",
        "--warn-status",
        help="OpenMetadata status of a WARN: Aborted (no incident) or Failed (opens an incident).",
    ),
    via: str = typer.Option("port-forward", "--via", help="Reach the ops bucket by 'port-forward' or 'direct'."),
) -> None:
    """Publish a stage's quality results to OpenMetadata from the ops bucket, without rerunning the pipeline.

    Safe to repeat: a result already in OpenMetadata is not published again. A stage without governance is a
    no-op that makes no OpenMetadata or bucket request.
    """
    from openlakeforge_core.quality_sync import OmStatus
    from openlakeforge_services.base import ServiceClientError
    from openlakeforge_services.openmetadata import OpenMetadataClient
    from openlakeforge_services.quality_publisher import StoreUnavailableError

    from olf.infra import k8s
    from olf.infra.artifact_store import ArtifactStoreError, artifact_storage_client
    from olf.infra.commands.runtime import provider_contract_environment
    from olf.infra.openmetadata._quality_sync import QualitySyncError, run_quality_sync
    from olf.infra.openmetadata._quality_writer import JWT_KEY, writer_credentials

    if warn_status not in {status.value for status in OmStatus if status is not OmStatus.SUCCESS}:
        raise typer.Exit(code=fail("--warn-status must be Aborted or Failed: a WARN is never published as Success."))

    with provider_contract_environment(
        provider=provider,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
        stage=stage,
    ) as contract:
        governance = _governance_service(contract)

        @contextmanager
        def open_catalog(catalog_stage: str) -> Iterator[OpenMetadataClient]:
            with governance.url(contract) as route:
                # The stage's quality writer, the identity its scheduled pass publishes with (ADR 0007).
                token = k8s.secret_value(
                    writer_credentials(catalog_stage), JWT_KEY, governance.namespace, environ=contract
                )
                yield route.client(OpenMetadataClient, token=token)

        try:
            outcome = run_quality_sync(
                contract,
                open_store=lambda bucket: artifact_storage_client(via, bucket, environ=contract),
                open_catalog=open_catalog,
                limit=limit,
                run_id=run_id or None,
                retry_failed=retry_failed,
                warn_status=OmStatus(warn_status),
            )
        except (QualitySyncError, ArtifactStoreError, StoreUnavailableError, k8s.KubectlError) as exc:
            raise typer.Exit(code=fail(str(exc))) from exc
        except ServiceClientError as exc:
            raise typer.Exit(code=fail(f"OpenMetadata request failed: HTTP {exc.status or 'no response'}")) from exc
    if outcome.report is None:
        typer.echo(f"Stage {outcome.stage} has no governance layer; nothing to publish.")
        return
    report = outcome.report
    summary = report.summary
    typer.echo(
        f"Stage {outcome.stage}: {report.runs_examined} runs, {report.published} published, "
        f"{report.deferred} waiting for table registration, {report.failed} failed, {report.skipped} skipped."
    )
    if summary is not None:
        lag = "none" if summary.lag_seconds is None else f"{summary.lag_seconds}s"
        typer.echo(f"Pending {summary.pending}, given up {summary.failed}, sync lag {lag}.")
    if report.interrupted:
        raise typer.Exit(code=fail("OpenMetadata or the bucket stopped answering; the rest stays pending."))


def _stage_values(flag: str, values: list[str] | None, stages: tuple[str, ...]) -> dict[str, str]:
    """Parse repeated STAGE=VALUE options, each stage at most once and one of `stages`."""
    parsed: dict[str, str] = {}
    for item in values or ():
        stage, _, value = item.partition("=")
        if stage not in stages or stage in parsed or not value:
            raise typer.Exit(code=fail(f"{flag} {item!r}: expected STAGE=VALUE, STAGE once and one of {stages}."))
        parsed[stage] = value
    return parsed


@app.command("bootstrap")
def openmetadata_bootstrap(
    provider: str = typer.Option("", "--provider", help=PROVIDER_HELP),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    om_url: str = typer.Option("", "--om-url", help="OpenMetadata URL; default is a port-forward to the service."),
    sso: bool = typer.Option(
        False, "--sso", help="Also create the admin automation bot and the read-only signup baseline SSO needs."
    ),
    catalog_service: str = typer.Option(
        "", "--catalog-service", help="Also seed OpenLineage, this lakehouse database service and its databases."
    ),
    catalog_display: str = typer.Option("", "--catalog-display", help="Display name of the catalog service."),
    trino_namespace: Annotated[
        list[str] | None,
        typer.Option(
            "--trino-namespace",
            help="A `trino://host:port` namespace, per stage and port; repeatable, first is the address.",
        ),
    ] = None,
    pipeline_service: str = typer.Option("", "--pipeline-service", help="OpenLineage's default pipeline service."),
    database: Annotated[
        list[str] | None, typer.Option("--database", help="A governed stage's catalog database; repeatable.")
    ] = None,
    schema: Annotated[
        list[str] | None, typer.Option("--schema", help="A schema every stage database holds; repeatable.")
    ] = None,
    dagster_url: Annotated[
        list[str] | None,
        typer.Option(
            "--dagster-url",
            help="STAGE=URL of a governed stage's Dagster webserver; repeatable. Also removes other stages' services.",
        ),
    ] = None,
    superset_url: Annotated[
        list[str] | None, typer.Option("--superset-url", help="STAGE=URL of a stage's Superset; repeatable.")
    ] = None,
    superset_namespace: Annotated[
        list[str] | None,
        typer.Option("--superset-namespace", help="STAGE=NAMESPACE holding that Superset's admin Secret; repeatable."),
    ] = None,
) -> None:
    """Create the admin password, bots and, with --catalog-service, the catalog entities. Idempotent.

    With --dagster-url, registers each governed stage's Dagster and Superset services with paused
    ingestion pipelines. Also removes the unqualified `dagster` and `superset` services registered
    before #131.
    """
    from olf.infra import k8s
    from olf.infra import openmetadata as om
    from olf.infra.access import Route
    from olf.infra.commands.runtime import provider_contract_environment
    from olf.infra.openmetadata._catalog_seed import CatalogSeed
    from olf.infra.openmetadata._platform import run_bootstrap
    from olf.infra.openmetadata._stage_services import STAGES, StageServices

    seed = (
        CatalogSeed(
            service=catalog_service,
            display_name=catalog_display or catalog_service,
            trino_namespaces=tuple(trino_namespace or ()),
            pipeline_service=pipeline_service,
            databases=tuple(database or ()),
            schemas=tuple(schema or ()),
        )
        if catalog_service
        else None
    )
    if seed and not (trino_namespace and pipeline_service):
        raise typer.Exit(code=fail("--catalog-service needs --trino-namespace and --pipeline-service."))
    dagster_urls = _stage_values("--dagster-url", dagster_url, STAGES)
    superset_urls = _stage_values("--superset-url", superset_url, tuple(dagster_urls))
    superset_namespaces = _stage_values("--superset-namespace", superset_namespace, tuple(superset_urls))
    if superset_namespaces.keys() != superset_urls.keys():
        raise typer.Exit(code=fail("Every --superset-url needs a --superset-namespace for the same stage."))
    stages = [
        StageServices(stage, url, superset_urls.get(stage, ""), superset_namespaces.get(stage, ""))
        for stage, url in dagster_urls.items()
    ]

    def run(route: Route, namespace: str) -> None:
        run_bootstrap(route, namespace, email=email, sso=sso, seed=seed, stages=stages, environ=contract)

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


def bootstrap_from_contract(contracts: ProviderContracts, *, environ: Mapping[str, str]) -> None:
    """Run the bootstrap with inputs read from the applied contract, then copy the ingestion-bot Secret
    into the governed stages' namespaces. With no governed stage there is no OpenMetadata to bootstrap,
    and the stages' OpenMetadata credentials are retired instead.

    A stage that is not governed loses the Trino crawl and its Polaris credential either way: the
    `openmetadata` principal outlives the last governed stage, so its copies must not.

    SSO follows the ConfigMap the governance module publishes only when an issuer is configured.
    """
    from olf.infra import k8s
    from olf.infra import openmetadata as om
    from olf.infra.openmetadata._platform import bootstrap_inputs, retire_governance, run_bootstrap
    from olf.infra.openmetadata._replication import (
        replicate_ingestion_bot,
        replication_namespaces,
        revoke_trino_crawl,
    )
    from olf.infra.openmetadata._sso import CONFIGMAP

    governed, revoked = replication_namespaces(contracts)
    try:
        revoke_trino_crawl(config.shared_namespace(environ=environ), revoked, environ=environ)
    except k8s.KubectlError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    inputs = bootstrap_inputs(contracts)
    governance = _governance_service(environ)
    email = config.env("OPENMETADATA_ADMIN_EMAIL", "admin@open-metadata.org", environ=environ)
    if inputs is None:
        try:
            present = k8s.resource_exists("service", governance.service, governance.namespace, environ=environ)
            retire_governance(
                governance.namespace,
                revoked,
                reach=(lambda: governance.url(environ)) if present else None,
                email=email,
                environ=environ,
            )
        except k8s.KubectlError as exc:
            raise typer.Exit(code=fail(str(exc))) from exc
        return
    seed, stages = inputs
    log.step("Bootstrapping OpenMetadata...")
    try:
        sso = k8s.configmap_data(CONFIGMAP, governance.namespace, environ=environ) is not None
        with governance.url(environ) as route:
            run_bootstrap(
                route,
                governance.namespace,
                email=email,
                sso=sso,
                seed=seed,
                stages=stages,
                environ=environ,
            )
        replicate_ingestion_bot(governance.namespace, governed, revoked, environ=environ)
    except (om.OpenMetadataError, k8s.KubectlError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


@app.command("list-domains")
def openmetadata_list_domains(
    provider: str = typer.Option("", "--provider", help=PROVIDER_HELP),
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
                        f"{namespace}. The OpenMetadata bootstrap writes it; rerun `olf infra deploy` to recreate it."
                    )
                )
            return route.client(OpenMetadataClient, token=token).list_domains()
    except k8s.KubectlError as exc:
        raise typer.Exit(
            code=fail(f"OpenMetadata is not reachable in namespace {namespace}; is the platform deployed? {exc}")
        ) from exc
    except ServiceClientError as exc:
        raise typer.Exit(code=fail(f"OpenMetadata did not list its domains: {exc}")) from exc
