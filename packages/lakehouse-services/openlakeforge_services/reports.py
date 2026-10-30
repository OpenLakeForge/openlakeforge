"""Superset report bundles: build, import and export (ADR 0021).

A report bundle is a source-controlled Superset asset export. Importing one
rewrites its database connection and schemas for the target stage; exporting
one puts them back to the stage-neutral form a bundle is checked in with.
Callers pass a logged-in `SupersetClient` and every credential.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

from .progress import SILENT, Progress
from .superset import SupersetClient

# Where the superset module mounts the `olf-local-ca` ConfigMap in the Superset pods.
SUPERSET_QUERY_CA = "/etc/olf/query-ca/ca.crt"
# Superset's per-database `extra` while Trino serves HTTPS (ADR 0016). Export
# strips exactly these keys, so they live beside the code that adds them.
TLS_EXTRA = {"engine_params": {"connect_args": {"http_scheme": "https", "verify": SUPERSET_QUERY_CA}}}
# The placeholder a checked-in database YAML carries. `build_report_bundle`
# replaces it with the target stage's connection, and `unpack_export_bundle`
# puts it back so an exported bundle promotes unchanged.
STAGE_NEUTRAL_SQLALCHEMY_URI = "trino://superset@trino:8080/iceberg"
# Superset creates this role empty (images/superset/olf_oidc.py); analysts and
# viewers read nothing until it holds the imported schemas.
DATA_ACCESS_ROLE = "olf_data_access"


@dataclass(frozen=True)
class ReportBundle:
    root: str
    name: str


def bundle_identity(report_source_dir: str) -> ReportBundle:
    """Derive the bundle root/name from the report source directory path."""
    root = re.sub(r"^lakehouse_code/dashboards/superset/", "", report_source_dir)
    root = re.sub(r"/metadata\.ya?ml$", "", root)
    root = root.replace("/", "_")
    root = f"{root}_superset_bundle"
    return ReportBundle(root=root, name=f"{root}.zip")


REPORT_YAML_SUFFIXES = {".yaml", ".yml"}


def discover_dashboard_files(report_dir: Path) -> list[Path]:
    """List a report bundle's dashboard export files, matching what packaging accepts."""
    dashboards_dir = report_dir / "dashboards"
    return sorted(
        path for path in dashboards_dir.glob("*") if path.is_file() and path.suffix.lower() in REPORT_YAML_SUFFIXES
    )


def build_report_bundle(
    source_dir: Path,
    bundle_path: Path,
    bundle_root: str,
    sqlalchemy_uri: str,
    *,
    schema_prefix: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Zip the report YAML, rewriting the database sqlalchemy_uri and dataset schemas in place.

    AWS Glue's shared default catalog (no per-stage custom catalog available)
    needs every physical schema prefixed with the stage's own catalog_name to
    stay collision-free (olf.infra.contracts.build_contract_env's namespace_prefix).
    The checked-in dataset YAML declares the bare logical schema
    (e.g. `order_revenue_gold`), so it has to be rewritten the same way the
    database YAML's sqlalchemy_uri already is - otherwise the dashboard
    imports fine but queries a schema that does not exist.
    """
    with ZipFile(bundle_path, "w", ZIP_DEFLATED) as bundle:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
                continue
            relative = path.relative_to(source_dir).as_posix()
            archive_name = PurePosixPath(bundle_root, relative).as_posix()
            rewrite_database = relative.startswith("databases/")
            rewrite_dataset = bool(schema_prefix) and relative.startswith("datasets/")
            if not rewrite_database and not rewrite_dataset:
                bundle.write(path, archive_name)
                continue

            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError(f"{relative}: is not a YAML mapping")
            if rewrite_database:
                document["sqlalchemy_uri"] = sqlalchemy_uri
                if extra:
                    document["extra"] = {**(document.get("extra") or {}), **extra}
            else:
                schema = document.get("schema")
                if not isinstance(schema, str) or not schema:
                    raise ValueError(f"{relative}: has no schema to prefix")
                document["schema"] = f"{schema_prefix}{schema}"
            bundle.writestr(archive_name, yaml.safe_dump(document, sort_keys=False))


def _neutral_database(document: dict[str, Any]) -> None:
    document["sqlalchemy_uri"] = STAGE_NEUTRAL_SQLALCHEMY_URI
    extra = document.get("extra")
    engine_params = extra.get("engine_params") if isinstance(extra, dict) else None
    connect_args = engine_params.get("connect_args") if isinstance(engine_params, dict) else None
    if not isinstance(engine_params, dict) or not isinstance(connect_args, dict):
        return
    for key, value in TLS_EXTRA["engine_params"]["connect_args"].items():
        if connect_args.get(key) == value:
            del connect_args[key]
    if not connect_args:
        del engine_params["connect_args"]


def _export_member(relative: PurePosixPath, content: bytes, schema_prefix: str) -> bytes:
    """Undo `build_report_bundle`'s stage rewrite on one exported asset."""
    kind = relative.parts[0]
    if kind not in {"databases", "datasets"}:
        return content
    document = yaml.safe_load(content.decode("utf-8"))
    if not isinstance(document, dict):
        return content
    if kind == "databases":
        _neutral_database(document)
    else:
        schema = document.get("schema")
        if not (schema_prefix and isinstance(schema, str) and schema.startswith(schema_prefix)):
            return content
        document["schema"] = schema.removeprefix(schema_prefix)
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


def unpack_export_bundle(bundle_path: Path, target_dir: Path, *, schema_prefix: str = "") -> None:
    """Replace managed report assets in the source tree from an export zip.

    Superset exports what it holds, which is the stage connection and, on AWS,
    the stage-prefixed schemas that `build_report_bundle` imported. Both are
    put back to their stage-neutral form, or report validation rejects the
    bundle as stage-bound.
    """
    managed_names = {"metadata.yaml", "databases", "datasets", "charts", "dashboards"}
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in managed_names:
        path = target_dir / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    with ZipFile(bundle_path) as bundle:
        for member in bundle.namelist():
            member_path = PurePosixPath(member)
            if (
                len(member_path.parts) < 2
                or member_path.name.startswith(".")
                or member_path.suffix.lower() not in {".yaml", ".yml"}
            ):
                continue
            relative = PurePosixPath(*member_path.parts[1:])
            destination = target_dir / Path(relative.as_posix())
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_export_member(relative, bundle.read(member), schema_prefix))


def canonical_uuid(value: object) -> str | None:
    """A uuid in its canonical spelling, or None if it is not one.

    Normalized rather than rejected: Superset resolves `A1B2-...` and
    `a1b2-...` to the same asset, so a bundle spelling one in either case is
    legitimate and must not be refused. Comparing the raw strings instead
    would let two bundles claim one identity in different cases and pass as
    distinct, which is the collision the uniqueness rule exists to catch.
    """
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def bundle_schema_perms(bundle_path: Path) -> set[str]:
    """The `[database].[schema]` view menus of every dataset the bundle imports.

    Report datasets are the Gold marts a dashboard reads, so the bundle (after
    `build_report_bundle` applied any schema prefix) is the source of truth for
    which schemas an import exposes.
    """
    databases: dict[str, str] = {}
    datasets: list[dict[str, Any]] = []
    with ZipFile(bundle_path) as bundle:
        for name in bundle.namelist():
            parts = PurePosixPath(name).parts
            if (
                len(parts) < 3
                or parts[1] not in {"databases", "datasets"}
                or not name.lower().endswith((".yaml", ".yml"))
            ):
                continue
            document = yaml.safe_load(bundle.read(name))
            if not isinstance(document, dict):
                continue
            if parts[1] == "databases":
                databases[canonical_uuid(document["uuid"]) or ""] = str(document["database_name"])
            else:
                datasets.append(document)
    return {
        f"[{databases[ref]}].[{d['schema']}]"
        for d in datasets
        if d.get("schema") and (ref := canonical_uuid(d.get("database_uuid"))) in databases
    }



def import_reports(
    client: SupersetClient,
    repo_root: Path,
    report_dirs: Sequence[str],
    *,
    work_dir: Path,
    sqlalchemy_uri: str,
    schema_prefix: str = "",
    extra: dict[str, Any] | None = None,
    database_password: str = "",
    progress: Progress = SILENT,
) -> None:
    """Build and import each report bundle, then grant the data-access role its schemas."""
    work_dir.mkdir(parents=True, exist_ok=True)
    schema_perms: set[str] = set()
    for report_dir in report_dirs:
        source_dir = repo_root / report_dir
        if not (source_dir / "metadata.yaml").is_file():
            raise RuntimeError(f"missing Superset report metadata at {report_dir}/metadata.yaml")

        identity = bundle_identity(report_dir)
        bundle_path = work_dir / identity.name
        build_report_bundle(
            source_dir, bundle_path, identity.root, sqlalchemy_uri, schema_prefix=schema_prefix, extra=extra
        )

        progress.step(f"Importing Superset report assets from {bundle_path}")
        client.import_assets(bundle_path, database_password=database_password)
        progress.info(f"Deployed Superset report assets from {report_dir}")
        schema_perms |= bundle_schema_perms(bundle_path)
    if schema_perms:
        # The role exists only when Superset runs with sign-in (identity.oidc).
        if not client.grant_permissions(DATA_ACCESS_ROLE, "schema_access", sorted(schema_perms)):
            progress.info("Superset sign-in is off; no data-access role to grant")


def export_report(
    client: SupersetClient,
    dashboard: str,
    *,
    bundle_path: Path,
    target_dir: Path,
    schema_prefix: str = "",
    progress: Progress = SILENT,
) -> None:
    """Export one dashboard into `target_dir` as a stage-neutral assets bundle."""
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    progress.step(f"Exporting dashboard '{dashboard}' from Superset")
    bundle_path.write_bytes(client.export_dashboard(dashboard))
    unpack_export_bundle(bundle_path, target_dir, schema_prefix=schema_prefix)
    # A dashboard export declares `type: Dashboard`; a bundle is imported as
    # assets, through `/api/v1/assets/import/`, which accepts nothing else.
    metadata_path = target_dir / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["type"] = "assets"
    metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")
