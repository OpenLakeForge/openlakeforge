"""Iceberg catalog namespace reconciliation."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import typer

from olf.common import config, log
from olf.common.log import fail

app = typer.Typer(help="Iceberg catalog namespace reconciliation.")


def _reconcile_and_report(
    label: str,
    client: Any,
    existing: dict,
    desired: tuple,
    *,
    dry_run: bool,
    prune: bool,
    namespace_prefix: str = "",
) -> None:
    from olf.infra import catalog as catalog_module

    plan = catalog_module.plan_namespace_sync(existing, desired, prune=prune, namespace_prefix=namespace_prefix)
    typer.echo(catalog_module.render_plan(plan, prune=prune))
    if dry_run:
        typer.echo("Dry run: the catalog was not changed.")
        return
    if plan.is_empty:
        typer.echo(f"{label} namespaces already match the descriptors.")
        return
    catalog_module.apply_namespace_sync(client, plan)
    typer.echo(
        f"Synced {label} namespaces: {len(plan.create)} created, {len(plan.adopt)} adopted, "
        f"{len(plan.update)} updated, {len(plan.delete)} metadata-removed."
    )


def _sync_polaris_namespaces(*, desired: tuple, dry_run: bool, prune: bool) -> None:
    from olf.infra import catalog as catalog_module
    from olf.infra import k8s
    from olf.infra import polaris as polaris_module

    rest_uri = config.env("OPENLAKEFORGE_CATALOG_REST_URI", "http://polaris.olf-system:8181/api/catalog")
    parsed = urlparse(rest_uri)
    if not parsed.hostname:
        raise typer.Exit(code=fail(f"OPENLAKEFORGE_CATALOG_REST_URI {rest_uri!r} has no host to port-forward to."))
    # The contract qualifies a shared service as `<service>.<namespace>` so
    # stage-scoped pods can resolve it; `kubectl port-forward` wants the two
    # apart again.
    service, _, service_namespace = parsed.hostname.partition(".")
    remote_port = parsed.port or 8181

    # The catalog runs in the shared namespace; its deployer credentials are
    # replicated into the stage namespace this command was invoked for.
    namespace = config.namespace()
    catalog_namespace = service_namespace or config.shared_namespace()
    secret_name = config.env("OPENLAKEFORGE_CATALOG_DEPLOYER_CREDENTIALS_SECRET_NAME", "polaris-deployer-creds")
    client_id_key = config.env("OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_ID_KEY", "POLARIS_DEPLOYER_CLIENT_ID")
    client_secret_key = config.env(
        "OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_SECRET_KEY", "POLARIS_DEPLOYER_CLIENT_SECRET"
    )

    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    with k8s.port_forward(
        service, remote_port, catalog_namespace, log_path=f"{log_prefix}-polaris-port-forward.log"
    ) as local_port:
        client = polaris_module.PolarisClient(
            polaris_module.PolarisConfig(
                base_url=f"http://127.0.0.1:{local_port}",
                catalog_name=config.env("OPENLAKEFORGE_CATALOG_NAME", "lakehouse_dev"),
                client_id=k8s.secret_value(secret_name, client_id_key, namespace),
                client_secret=k8s.secret_value(secret_name, client_secret_key, namespace),
                oauth_scope=config.env("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE", "PRINCIPAL_ROLE:ALL"),
            )
        )
        try:
            client.login()
            _reconcile_and_report(
                "Polaris", client, client.list_namespaces(), desired, dry_run=dry_run, prune=prune
            )
        except (catalog_module.NamespaceSyncError, polaris_module.PolarisError) as exc:
            raise typer.Exit(code=fail(str(exc))) from exc


def _sync_glue_namespaces(*, desired: tuple, dry_run: bool, prune: bool, namespace_prefix: str) -> None:
    from olf.infra import catalog as catalog_module
    from olf.infra import glue as glue_module

    catalog_name = config.env("OPENLAKEFORGE_CATALOG_NAME", "lakehouse_dev")
    client = glue_module.GlueClient(
        glue_module.GlueConfig(
            catalog_id=config.env("OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID"),
            region=config.env("OPENLAKEFORGE_CATALOG_GLUE_REGION"),
            catalog_name=catalog_name,
        )
    )
    try:
        _reconcile_and_report(
            "Glue",
            client,
            client.list_namespaces(),
            desired,
            dry_run=dry_run,
            prune=prune,
            namespace_prefix=namespace_prefix,
        )
    except (catalog_module.NamespaceSyncError, glue_module.GlueError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def sync_namespaces(*, dry_run: bool, prune: bool | None) -> None:
    """Reconcile catalog namespaces (Polaris) or databases (Glue) with the domain descriptors.

    Phase 2 owns namespace lifecycle (ADR 0002), so this runs before any table
    is written. Plain function (not the typer command) so the full deploy
    flow - already inside a hydrated contract environment - can call it
    directly without re-resolving one.
    """
    from olf.infra import catalog as catalog_module

    provider = config.env("OPENLAKEFORGE_CATALOG_PROVIDER", "polaris")
    if prune is None:
        prune = config.truthy(config.env("OPENLAKEFORGE_CATALOG_PRUNE_NAMESPACES", "false"))
    catalog_name = config.env("OPENLAKEFORGE_CATALOG_NAME", "lakehouse_dev")
    # AWS Glue has no per-stage catalog (this account's Glue service refuses
    # to create custom/"native" catalogs) - every stage shares the account's
    # one default catalog, so its physical database names need a per-stage
    # prefix to stay collision-free, matching olf.infra.contracts.build_contract_env's
    # own namespace_prefix. Every other provider still gets a per-stage
    # catalog, so the catalog boundary alone is enough there.
    namespace_prefix = f"{catalog_name}_" if provider == "aws-glue" else ""
    desired = catalog_module.desired_namespaces(
        config.project_spec().root,
        bronze_bucket=config.env("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET", "lakehouse-bronze"),
        silver_bucket=config.env("OPENLAKEFORGE_STORAGE_SILVER_BUCKET", "lakehouse-silver"),
        gold_bucket=config.env("OPENLAKEFORGE_STORAGE_GOLD_BUCKET", "lakehouse-gold"),
        namespace_prefix=namespace_prefix,
    )

    if provider == "polaris":
        log.step(f"Reconciling {len(desired)} Polaris namespace(s) from the domain descriptors...")
        _sync_polaris_namespaces(desired=desired, dry_run=dry_run, prune=prune)
    elif provider == "aws-glue":
        log.step(f"Reconciling {len(desired)} Glue database(s) from the domain descriptors...")
        _sync_glue_namespaces(desired=desired, dry_run=dry_run, prune=prune, namespace_prefix=namespace_prefix)
    else:
        raise typer.Exit(code=fail(f"Catalog provider {provider!r} has no namespace reconciliation backend."))


@app.command("sync-namespaces")
def catalog_sync_namespaces(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan without changing the catalog."),
    prune: bool | None = typer.Option(
        None,
        "--prune/--no-prune",
        help="Remove managed metadata for undeclared products; object-store files are retained.",
    ),
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option("", "--project-root", help="Writable project root; defaults to the bundled demo."),
) -> None:
    """Hydrate the selected provider's contracts, then reconcile catalog namespaces."""
    from olf.infra.commands.runtime import provider_contract_environment

    with provider_contract_environment(
        provider=provider,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    ):
        sync_namespaces(dry_run=dry_run, prune=prune)
