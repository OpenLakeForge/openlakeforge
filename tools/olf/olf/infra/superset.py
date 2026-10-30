"""Superset report import and export for `olf infra`.

The operations themselves are `openlakeforge_services.reports`; this module
resolves what they need from the deployment: the stage's connection from the
applied contract, Superset's URL through a port-forward, and the admin and
Trino passwords from Kubernetes Secrets.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openlakeforge_services import reports
from openlakeforge_services.reports import TLS_EXTRA
from openlakeforge_services.superset import SupersetClient

from olf.common import config, layers, log
from olf.common.report_bundles import discover_report_dirs, validate_report_registry
from olf.infra import access, k8s
from olf.infra.contracts import CONTRACT_STAGE_ENV


class ReportStageError(RuntimeError):
    """A stage cannot serve a report import or export."""


SUPERSET_PASSWORD_MASK = "XXXXXXXXXX"
# Created in-cluster by the Superset module, release "superset".
ADMIN_SECRET = "superset-credentials"


@dataclass(frozen=True)
class StageReportTarget:
    """One stage's own Superset instance and Gold connection."""

    stage: str
    namespace: str
    sqlalchemy_uri: str
    schema_prefix: str
    # Superset's per-database `extra`: the HTTPS scheme and the CA to verify
    # against, set only while Trino serves HTTPS (ADR 0016).
    extra: dict[str, Any] | None = None
    # Secret in `namespace` holding this stage's Trino password; empty while
    # Trino takes no password.
    credential_secret: str = ""


def resolve_stage_report_target(environ: Mapping[str, str], *, stage: str = "") -> StageReportTarget:
    """Resolve one stage's Superset target from that stage's own contract bindings.

    Reads `OPENLAKEFORGE_KUBE_NAMESPACE` rather than `config.namespace()`,
    whose `NAMESPACE` fallback a caller can export, and builds the Trino URI
    from the contract rather than accepting a caller-exported one. Nothing
    derives a target from a selected-stage value, which would silently
    target another stage's instance.

    Analytics is a per-stage capability (ADR 0011): the v3 stage index emits
    `stages.<name>.reporting` only for an analytics-enabled stage, which
    reaches this environment as `OPENLAKEFORGE_ANALYTICS_ENABLED`. A stage
    without it has no Superset to talk to at all.
    """
    resolved_stage = stage or environ.get(CONTRACT_STAGE_ENV, "")
    if not layers.enabled(environ, "analytics"):
        raise ReportStageError(
            f"stage {resolved_stage!r} has analytics disabled: it provisions no Superset instance."
        )
    namespace = environ.get("OPENLAKEFORGE_KUBE_NAMESPACE", "")
    connection = {
        name: environ.get(name, "")
        for name in (
            "OPENLAKEFORGE_DBT_TRINO_USER",
            "OPENLAKEFORGE_QUERY_TRINO_HOST",
            "OPENLAKEFORGE_QUERY_TRINO_PORT",
            "OPENLAKEFORGE_QUERY_TRINO_CATALOG",
        )
    }
    missing = [name for name, value in (("OPENLAKEFORGE_KUBE_NAMESPACE", namespace), *connection.items()) if not value]
    if missing:
        raise ReportStageError(
            f"stage {resolved_stage!r} resolved no {', '.join(missing)}: deploy the platform for this stage first."
        )
    # Only a contract that was actually applied for this stage counts.
    # `build_contract_env` synthesizes a dev-shaped environment, and keeps any
    # caller-exported value, when the Terraform output is unavailable -- so a
    # shell still holding another deployment's `lakehouse_<stage>` bindings
    # would pass any check on the values themselves. The provenance marker is
    # written only when a contract was applied and unset otherwise.
    applied = environ.get(CONTRACT_STAGE_ENV, "")
    if not applied or applied != resolved_stage:
        raise ReportStageError(
            f"stage {resolved_stage!r} has no applied provider contract in this environment"
            + (f" (the applied contract serves {applied!r})" if applied else "")
            + ". Deploy the platform for this stage first."
        )
    # Built here from the contract's own fields rather than read from
    # OPENLAKEFORGE_QUERY_SQLALCHEMY_URI, which `build_contract_env` keeps
    # when the caller exported one. Any component of a kept URI can belong to
    # another stage or deployment -- the endpoint, the catalog, or the Trino
    # user whose catalog rules decide what SQL Lab on this connection can
    # read -- so none of it is trusted. Same shape `build_contract_env` uses.
    tls_port = environ.get("OPENLAKEFORGE_QUERY_TRINO_TLS_PORT", "")
    tls = bool(tls_port and environ.get("OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP"))
    credential_secret = environ.get("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "") if tls else ""
    # Superset's own placeholder, not a secret. The import's `passwords` field
    # fills the real value, but Database.set_sqlalchemy_uri then overwrites it
    # with the URI's password unless that is the placeholder (6.1.0), so a URI
    # without one wipes the password and the import's first Trino call gets 401.
    sqlalchemy_uri = "trino://{user}{mask}@{host}:{port}/{catalog}".format(
        mask=f":{SUPERSET_PASSWORD_MASK}" if credential_secret else "",
        user=connection["OPENLAKEFORGE_DBT_TRINO_USER"],
        host=connection["OPENLAKEFORGE_QUERY_TRINO_HOST"],
        port=tls_port if tls else connection["OPENLAKEFORGE_QUERY_TRINO_PORT"],
        catalog=connection["OPENLAKEFORGE_QUERY_TRINO_CATALOG"],
    )
    return StageReportTarget(
        stage=resolved_stage,
        namespace=namespace,
        sqlalchemy_uri=sqlalchemy_uri,
        schema_prefix=environ.get("OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX", ""),
        extra=TLS_EXTRA if tls else None,
        credential_secret=credential_secret,
    )


@contextlib.contextmanager
def superset_base_url(namespace: str) -> Iterator[str]:
    """Yield the base URL of this namespace's Superset API for the block."""
    log.step("Waiting for Superset web deployment...")
    k8s.wait_for_rollout("deployment/superset", namespace)
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    with access.service_url(
        f"stage/{config.env(CONTRACT_STAGE_ENV, 'dev')}/reporting",
        service="superset",
        remote_port=8088,
        namespace=namespace,
        log_path=f"{log_prefix}-superset-port-forward.log",
        shared_namespace=config.shared_namespace(),
    ) as url:
        yield url


def admin_password_for(namespace: str, override: str = "", *, kube_context: str | None = None) -> str:
    """The Superset admin password: an explicit override, else the Secret the Superset module's Job created."""
    return override or k8s.secret_value(ADMIN_SECRET, "admin-password", namespace, kube_context=kube_context)


def deploy_reports(
    repo_root: Path,
    namespace: str,
    sqlalchemy_uri: str,
    *,
    report_source_dir: str | None,
    declared_report_dirs: Sequence[str] | None = None,
    work_dir: Path,
    admin_username: str,
    admin_password: str,
    schema_prefix: str = "",
    extra: dict[str, Any] | None = None,
    credential_secret: str = "",
) -> None:
    # Computed before touching the cluster: a declared-but-unmounted bundle
    # must still fail loudly, and validate_report_registry already does that
    # (raises before returning) whenever declared_report_dirs is given, so
    # reaching the empty check below means every declared dashboard really
    # is missing -- i.e. none are declared at all (#228's scaffolded-but-
    # undeclared draft, or any project that simply has no dashboards yet).
    # That is a legitimate state, not a misconfiguration: importing nothing
    # is the correct outcome, not an error to wait for a live Superset
    # just to report.
    report_dirs = (
        [report_source_dir]
        if report_source_dir
        else validate_report_registry(repo_root, declared_report_dirs)
        if declared_report_dirs is not None
        else discover_report_dirs(repo_root)
    )
    if not report_dirs:
        log.step("No Superset report bundles are declared; nothing to import.")
        return

    trino_password = k8s.secret_value(credential_secret, "password", namespace) if credential_secret else ""
    with superset_base_url(namespace) as base_url:
        password = admin_password_for(namespace, admin_password)
        client = SupersetClient(base_url, username=admin_username, password=password)
        client.login()
        reports.import_reports(
            client,
            repo_root,
            report_dirs,
            work_dir=work_dir,
            sqlalchemy_uri=sqlalchemy_uri,
            schema_prefix=schema_prefix,
            extra=extra,
            database_password=trino_password,
            progress=log,
        )


def export_report(
    repo_root: Path,
    namespace: str,
    *,
    report_source_dir: str,
    bundle_name: str,
    work_dir: Path,
    admin_username: str,
    admin_password: str,
    dashboard: str,
    schema_prefix: str = "",
) -> None:
    with superset_base_url(namespace) as base_url:
        password = admin_password_for(namespace, admin_password)
        client = SupersetClient(base_url, username=admin_username, password=password)
        client.login()
        reports.export_report(
            client,
            dashboard,
            bundle_path=work_dir / bundle_name,
            target_dir=repo_root / report_source_dir,
            schema_prefix=schema_prefix,
            progress=log,
        )
    log.info(f"Exported Superset report assets to {report_source_dir}")
