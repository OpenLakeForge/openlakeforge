"""Superset report bundles: build, import and export (ADR 0021).

A report bundle is a source-controlled Superset asset export. Importing one
rewrites its database connection and schemas for the target stage; exporting
one puts them back to the stage-neutral form a bundle is checked in with.
Callers pass a logged-in `SupersetClient` and every credential.
"""

from __future__ import annotations

import io
import re
import shutil
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

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
# The platform API's Superset service user and role (ADR 0021, open item 3), created by Superset's init script
# (infra/terraform/modules/analytics/superset and access/platform-api, which name them the same). Re-importing a
# report asset needs ownership, so the import makes this user a co-owner of what it imports; exporting one needs
# data access, so the role gets the same schemas as DATA_ACCESS_ROLE.
PLATFORM_API_ROLE = "olf_platform_api"
PLATFORM_API_USERNAME = "olf-platform-api"


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
        # Superset 6.1.0 sets a dataset's catalog to its connection's default on any update when the
        # database has no multi-catalog (commands/dataset/update.py): a UI edit, or the platform API's
        # owner handover. That default is the stage's own catalog, so it is dropped; each stage's
        # connection supplies its own, as for a dataset imported without one.
        if document.get("catalog") is None and not (
            schema_prefix and isinstance(schema, str) and schema.startswith(schema_prefix)
        ):
            return content
        document.pop("catalog", None)
        if schema_prefix and isinstance(schema, str) and schema.startswith(schema_prefix):
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


class UploadRejected(ValueError):
    """An uploaded report is not one the API imports; the message says why and names no server detail."""


@dataclass(frozen=True)
class PreparedUpload:
    """A report a person uploaded, rewritten for one stage's Superset and ready for a sparse import."""

    bundle: bytes
    dashboards: tuple[str, ...]
    schema_perms: frozenset[str]


UPLOAD_ROOT = "report"
_UPLOAD_KINDS = {"datasets", "charts", "dashboards"}
# A report is a few dozen small YAML files. These bound what an upload may expand to, so a highly compressed
# archive cannot exhaust the memory of the host that unpacks it (the platform API runs in 192 MiB).
MAX_UPLOAD_MEMBERS = 500
MAX_UPLOAD_MEMBER_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 10 * 1024 * 1024


def _read_bounded(source: ZipFile, info: ZipInfo) -> bytes:
    """A member's content, never more than it declared or the per-member cap: the declared size is the uploader's."""
    limit = min(info.file_size, MAX_UPLOAD_MEMBER_BYTES)
    with source.open(info) as member:
        content = member.read(limit + 1)
    if len(content) > limit:
        raise UploadRejected(f"{info.filename} expands beyond its declared size")
    return content


def prepare_upload(archive: bytes, databases: dict[str, str], *, schema_prefix: str = "") -> PreparedUpload:
    """Rebuild an uploaded report directory (zipped from its root) as an assets bundle for a sparse import.

    Only `metadata.yaml` and the YAML under datasets/, charts/ and dashboards/
    are kept. Databases are never taken from an upload: a sparse import
    resolves each dataset's `database_uuid` among the databases the stage's
    Superset already holds (`databases`, uuid to name), so an upload cannot
    replace a connection, and a uuid that names none of them is refused.
    Dataset schemas get the stage's prefix, as `build_report_bundle` does.
    """
    try:
        source = ZipFile(io.BytesIO(archive))
        members = source.infolist()
    except (BadZipFile, ValueError) as exc:
        raise UploadRejected("the upload is not a zip archive") from exc
    # Checked on the declared sizes before anything is decompressed; _read_bounded holds each member to them.
    if len(members) > MAX_UPLOAD_MEMBERS:
        raise UploadRejected(f"the upload has more than {MAX_UPLOAD_MEMBERS} files")
    if any(info.file_size > MAX_UPLOAD_MEMBER_BYTES for info in members):
        raise UploadRejected(f"a file in the upload is over {MAX_UPLOAD_MEMBER_BYTES} bytes")
    if sum(info.file_size for info in members) > MAX_UPLOAD_TOTAL_BYTES:
        raise UploadRejected(f"the upload expands to over {MAX_UPLOAD_TOTAL_BYTES} bytes")
    dashboards: list[str] = []
    schema_perms: set[str] = set()
    out = io.BytesIO()
    with source, ZipFile(out, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{UPLOAD_ROOT}/metadata.yaml", yaml.safe_dump({"version": "1.0.0", "type": "assets"}))
        for info in members:
            name = info.filename
            path = PurePosixPath(name)
            if name.endswith("/") or path.name.startswith("."):
                continue
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise UploadRejected(f"{name!r} is not a path inside the report")
            if path.suffix.lower() not in {".yaml", ".yml"} or len(path.parts) < 2 or path.parts[0] not in _UPLOAD_KINDS:
                continue  # metadata.yaml is rewritten above; databases/ and anything else are never imported
            try:
                document = yaml.safe_load(_read_bounded(source, info))
            except (BadZipFile, zlib.error, yaml.YAMLError) as exc:
                raise UploadRejected(f"{name} cannot be read as YAML") from exc
            if not isinstance(document, dict):
                raise UploadRejected(f"{name} is not a YAML mapping")
            if path.parts[0] == "datasets":
                database = canonical_uuid(document.get("database_uuid"))
                schema = document.get("schema")
                if database not in databases:
                    raise UploadRejected(f"{name} uses a database this stage's Superset does not have")
                if not isinstance(schema, str) or not schema:
                    raise UploadRejected(f"{name} has no schema")
                document["schema"] = f"{schema_prefix}{schema}"
                schema_perms.add(f"[{databases[database]}].[{document['schema']}]")
            elif path.parts[0] == "dashboards" and (uuid := canonical_uuid(document.get("uuid"))):
                dashboards.append(uuid)
            bundle.writestr(f"{UPLOAD_ROOT}/{path.as_posix()}", yaml.safe_dump(document, sort_keys=False))
    if not dashboards:
        raise UploadRejected("the upload has no dashboard")
    return PreparedUpload(out.getvalue(), tuple(dashboards), frozenset(schema_perms))


def neutral_export(raw: bytes, *, schema_prefix: str = "") -> bytes:
    """Superset's export zip with each asset put back to its stage-neutral form, as `unpack_export_bundle` does.

    Done in memory: the platform API runs with a read-only root filesystem.
    The bundle keeps Superset's own top-level directory and declares
    `type: assets`, so `unpack_export_bundle` and an import both accept it.
    """
    out = io.BytesIO()
    with ZipFile(io.BytesIO(raw)) as source, ZipFile(out, "w", ZIP_DEFLATED) as bundle:
        for member in source.namelist():
            path = PurePosixPath(member)
            if len(path.parts) < 2 or path.name.startswith(".") or path.suffix.lower() not in {".yaml", ".yml"}:
                continue
            relative = PurePosixPath(*path.parts[1:])
            content = _export_member(relative, source.read(member), schema_prefix)
            if relative.as_posix() == "metadata.yaml":
                metadata = yaml.safe_load(content) or {}
                content = yaml.safe_dump({**metadata, "type": "assets"}, sort_keys=False).encode("utf-8")
            bundle.writestr(member, content)
    return out.getvalue()


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



def bundle_dashboard_uuids(bundle_path: Path) -> list[str]:
    """The uuid of each dashboard in a report bundle, in archive order."""
    uuids: list[str] = []
    with ZipFile(bundle_path) as bundle:
        for name in bundle.namelist():
            parts = PurePosixPath(name).parts
            if len(parts) < 3 or parts[1] != "dashboards" or not name.lower().endswith((".yaml", ".yml")):
                continue
            document = yaml.safe_load(bundle.read(name))
            if isinstance(document, dict) and (uuid := canonical_uuid(document.get("uuid"))):
                uuids.append(uuid)
    return uuids


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
    dashboards: list[str] = []
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
        dashboards += bundle_dashboard_uuids(bundle_path)
    if schema_perms:
        # The role exists only when Superset runs with sign-in (identity.oidc).
        if not client.grant_permissions(DATA_ACCESS_ROLE, "schema_access", sorted(schema_perms)):
            progress.info("Superset sign-in is off; no data-access role to grant")
        if client.grant_permissions(PLATFORM_API_ROLE, "schema_access", sorted(schema_perms)):
            progress.info(f"Granted {PLATFORM_API_ROLE} the report schemas, so the platform API can export them")
    _hand_over(client, dashboards, progress)


def _hand_over(client: SupersetClient, dashboards: Sequence[str], progress: Progress) -> None:
    """Make the platform API's service user a co-owner of every dashboard imported, and its charts and datasets.

    An import keeps the owners an asset has and adds the importer, so the user
    stays an owner across later imports. Without the platform API there is no
    such user, and nothing is handed over.
    """
    if not dashboards:
        return
    user = client.user_id(PLATFORM_API_USERNAME)
    if user is None:
        progress.info(f"Superset has no {PLATFORM_API_USERNAME} user; no report asset is handed over")
        return
    changed = sum(client.add_owner(dashboard, user) for dashboard in dashboards)
    progress.info(f"Made {PLATFORM_API_USERNAME} a co-owner of {changed} report asset(s)")


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
