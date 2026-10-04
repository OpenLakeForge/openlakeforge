"""Superset report import/export/validate commands."""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from openlakeforge_domain import inventory_for

from olf import config, log
from olf.commands._shared import fail

if TYPE_CHECKING:
    from olf.superset import StageReportTarget

app = typer.Typer(help="Deprecated aliases of the `olf report` commands.")
report_app = typer.Typer(help="Source-controlled Superset report bundles.")

_REPORT_BUNDLE_ROOT = "lakehouse_code/dashboards/superset"


def _validate_report_target_dir(project_root: Path, override: str) -> None:
    """Refuse a SUPERSET_REPORT_SOURCE_DIR that can't be a real bundle directory.

    `export_report` -> `unpack_export_bundle` deletes metadata.yaml/databases/
    datasets/charts/dashboards under whatever this resolves to, so this is a
    trust boundary, not a validation nicety. `project_root / override` silently
    drops `project_root` when `override` is absolute (`PurePath.__truediv__`),
    and a `..`-bearing override can walk out of the report tree without ever
    tripping an unresolved string comparison -- both have to be resolved and
    checked for real containment.
    """
    report_root = (project_root / _REPORT_BUNDLE_ROOT).resolve()
    target = (project_root / override).resolve()
    # Direct child, not any descendant: a bundle is one directory under the
    # report root, so `.../superset/orders/datasets` is a bundle's *contents*.
    # Accepting it would make `unpack_export_bundle` treat that subtree as a
    # bundle root and delete the managed entries inside it.
    if target.parent != report_root:
        raise typer.BadParameter(
            f"SUPERSET_REPORT_SOURCE_DIR {override!r} must be a bundle directory directly under {_REPORT_BUNDLE_ROOT}"
        )


@report_app.command("validate")
def report_validate(
    dashboard: str = typer.Argument(
        "", help="One dashboard declared in lakehouse.yaml; defaults to every declared dashboard."
    ),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Check report bundles against the promotion contract: stable identities,
    resolvable references, and no stage-bound or workspace-bound values."""
    from olf import superset
    from olf.commands._project import writable_project_root

    try:
        root = writable_project_root(project_root)
        declared = {item.name: item.report_source_dir for item in inventory_for(root).dashboards}
        if dashboard and dashboard not in declared:
            raise typer.BadParameter(f"{dashboard!r} is not a dashboard declared in lakehouse.yaml")
        # Without a named dashboard this also enforces descriptor/tree parity:
        # an undeclared bundle is never packaged, so validating only what is
        # declared would report a project clean that cannot promote its tree.
        selected = (
            [declared[dashboard]] if dashboard else superset.validate_report_registry(root, tuple(declared.values()))
        )
        errors = superset.validate_report_bundles(root, selected)
    except RuntimeError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for error in errors:
        typer.echo(error)
    if errors:
        raise typer.Exit(code=1)
    typer.echo(f"{len(selected)} report bundle(s) are promotable.")


@report_app.command("import")
def report_import(
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option(
        "", "--stage", help="Stage whose Superset receives the reports: dev, uat, or prod. Defaults to dev."
    ),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Build and import reports using the selected provider's Terraform contracts."""
    from olf.commands.runtime import provider_contract_environment

    with provider_contract_environment(
        provider=provider,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
        stage=stage,
    ):
        deploy_superset_reports(stage=stage)


def deploy_superset_reports(stage: str = "") -> None:
    """Build and import source-controlled Superset report bundles."""
    from olf import superset

    project = config.project_spec()
    target = _report_target(stage)
    inventory = inventory_for(project.root)
    declared_report_dirs = tuple(dashboard.report_source_dir for dashboard in inventory.dashboards)
    override = os.environ.get("SUPERSET_REPORT_SOURCE_DIR") or None
    if override is not None and override not in declared_report_dirs:
        raise typer.BadParameter(f"SUPERSET_REPORT_SOURCE_DIR {override!r} is not declared in lakehouse.yaml")
    log.step(f"Importing Superset reports into stage '{target.stage}' (namespace {target.namespace})")
    superset.deploy_reports(
        project.root,
        target.namespace,
        target.sqlalchemy_uri,
        report_source_dir=override,
        declared_report_dirs=declared_report_dirs,
        work_dir=Path(config.env("SUPERSET_REPORT_WORK_DIR", ".tmp/superset-reports")),
        admin_username=config.env("SUPERSET_ADMIN_USERNAME", "admin"),
        admin_password=config.env("SUPERSET_ADMIN_PASSWORD", "admin"),
        schema_prefix=target.schema_prefix,
    )


@report_app.command("export")
def report_export(
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option(
        ...,
        "--stage",
        help="Stage whose Superset is exported from: dev, uat, or prod. Authoring happens in shared DEV.",
    ),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Export reports using the selected provider's Terraform contracts."""
    from olf.commands.runtime import provider_contract_environment

    with provider_contract_environment(
        provider=provider,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
        stage=stage,
    ):
        export_superset_reports(stage=stage)


def export_superset_reports(stage: str = "") -> None:
    """Export a live Superset dashboard back into a source-controlled bundle."""
    import yaml
    from openlakeforge_domain import Dashboard

    from olf import superset

    project = config.project_spec()
    target = _report_target(stage)
    inventory = inventory_for(project.root)
    override = os.environ.get("SUPERSET_REPORT_SOURCE_DIR") or None
    if override is not None:
        _validate_report_target_dir(project.root, override)
    if inventory.dashboards:
        # Unchanged from before #229: the first declared dashboard is always
        # the bundle-name source, even when an override targets a different
        # (declared or undeclared) bundle.
        default_dashboard = inventory.dashboards[0]
        report_source_dir = override or default_dashboard.report_source_dir
    elif override:
        # Nothing declared yet (e.g. a scaffolded --with-report draft, #205):
        # a named target is still exportable, but only if it already exists,
        # so a typo doesn't silently create a bundle.
        if not (project.root / override).is_dir():
            raise typer.BadParameter(f"SUPERSET_REPORT_SOURCE_DIR {override!r} does not exist")
        report_source_dir = override
        default_dashboard = Dashboard(name=Path(override).name, products=())
    else:
        raise typer.BadParameter(
            "lakehouse.yaml declares no dashboard to export; "
            "set SUPERSET_REPORT_SOURCE_DIR to target an undeclared bundle"
        )

    if os.environ.get("SUPERSET_DASHBOARD_TITLE"):
        raise typer.BadParameter(
            "SUPERSET_DASHBOARD_TITLE is no longer read: export selects the dashboard by stable identity. "
            "Set SUPERSET_DASHBOARD to its uuid or slug instead."
        )

    def _default_dashboard() -> str:
        # The checked-in bundle's own uuid, which survives a rename in Superset.
        for dashboard_file in superset.discover_dashboard_files(project.root / report_source_dir):
            document = yaml.safe_load(dashboard_file.read_text())
            if isinstance(document, dict) and document.get("uuid"):
                return str(document["uuid"])
        # A draft bundle has nothing to read yet, and guessing from a title
        # or name would be the rename-fragile lookup this replaces.
        raise typer.BadParameter(
            f"{report_source_dir} has no exported dashboard to identify; "
            "set SUPERSET_DASHBOARD to the uuid or slug of the dashboard in Superset"
        )

    log.step(f"Exporting Superset reports from stage '{target.stage}' (namespace {target.namespace})")
    superset.export_report(
        project.root,
        target.namespace,
        report_source_dir=report_source_dir,
        bundle_name=config.env(
            "SUPERSET_REPORT_EXPORT_BUNDLE_NAME", default_dashboard.superset_export_bundle_name
        ),
        work_dir=Path(config.env("SUPERSET_REPORT_WORK_DIR", ".tmp/superset-reports")),
        admin_username=config.env("SUPERSET_ADMIN_USERNAME", "admin"),
        admin_password=config.env("SUPERSET_ADMIN_PASSWORD", "admin"),
        dashboard=os.environ.get("SUPERSET_DASHBOARD") or _default_dashboard(),
    )


def _deprecated_alias(command: Callable[..., None], replacement: str) -> Callable[..., None]:
    # functools.wraps exposes the command's signature, so Typer builds the
    # alias with the same options.
    @functools.wraps(command)
    def alias(**kwargs: object) -> None:
        log.warn(f"this command is deprecated; use `{replacement}` instead.")
        command(**kwargs)

    return alias


app.command("deploy-reports", help="Deprecated alias of `olf report import`.")(
    _deprecated_alias(report_import, "olf report import")
)
app.command("export-reports", help="Deprecated alias of `olf report export`.")(
    _deprecated_alias(report_export, "olf report export")
)


def _report_target(stage: str) -> StageReportTarget:
    from olf import superset

    try:
        return superset.resolve_stage_report_target(os.environ, stage=stage)
    except superset.ReportStageError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
