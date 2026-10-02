"""Superset report bundle build, import, and export.

Replaces scripts/local/artifacts/superset-reports-deploy.sh and
superset-reports-export.sh. Bundle building and unpacking are pure local
operations; the import and export commands run inside the Superset pod (they
need Superset's own interpreter and database), so those bodies stay as scripts
executed through `kubectl exec`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

from olf import k8s, layers, log
from olf.contracts import CONTRACT_STAGE_ENV
from olf.profile import StageName

REPORTS_MOUNT_PATH_DEFAULT = "/app/openlakeforge/reports"

# In-pod importer. Runs in the Superset interpreter; argv: <remote_bundle> <username>.
_IMPORT_SCRIPT = """
import sys
from zipfile import ZipFile

from flask import g

from superset.app import create_app

bundle_path = sys.argv[1]
username = sys.argv[2]

app = create_app()
with app.app_context():
    from superset import security_manager
    from superset.commands.importers.v1.assets import ImportAssetsCommand
    from superset.commands.importers.v1.utils import get_contents_from_bundle

    user = security_manager.find_user(username=username)
    if user is None:
        raise SystemExit(f"Superset user '{username}' does not exist")

    g.user = user
    with ZipFile(bundle_path) as bundle:
        contents = get_contents_from_bundle(bundle)
    ImportAssetsCommand(contents).run()
"""

# In-pod exporter. argv: <remote_bundle> <username> <dashboard_title> <bundle_root>.
_EXPORT_SCRIPT = """
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from flask import g

from superset.app import create_app

bundle_path = sys.argv[1]
username = sys.argv[2]
dashboard_title = sys.argv[3]
bundle_root = sys.argv[4]

app = create_app()
with app.app_context():
    from superset import security_manager
    from superset.commands.dashboard.export import ExportDashboardsCommand
    from superset.extensions import db
    from superset.models.dashboard import Dashboard

    user = security_manager.find_user(username=username)
    if user is None:
        raise SystemExit(f"Superset user '{username}' does not exist")
    g.user = user

    dashboard_ids = [
        dashboard_id
        for (dashboard_id,) in db.session.query(Dashboard.id)
        .filter(Dashboard.dashboard_title == dashboard_title)
        .all()
    ]
    if not dashboard_ids:
        raise SystemExit(f"Superset dashboard '{dashboard_title}' does not exist")

    with ZipFile(bundle_path, "w", ZIP_DEFLATED) as bundle:
        for file_name, file_content in ExportDashboardsCommand(dashboard_ids).run():
            content = file_content()
            if file_name == "metadata.yaml":
                metadata = yaml.safe_load(content)
                metadata["type"] = "assets"
                content = yaml.safe_dump(metadata, sort_keys=False)
            with bundle.open(f"{bundle_root}/{file_name}", "w") as fp:
                fp.write(content.encode())
"""


class ReportStageError(RuntimeError):
    """A stage cannot serve a report import or export."""


@dataclass(frozen=True)
class StageReportTarget:
    """One stage's own Superset instance and Gold connection."""

    stage: str
    namespace: str
    sqlalchemy_uri: str
    schema_prefix: str


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
    sqlalchemy_uri = "trino://{user}@{host}:{port}/{catalog}".format(
        user=connection["OPENLAKEFORGE_DBT_TRINO_USER"],
        host=connection["OPENLAKEFORGE_QUERY_TRINO_HOST"],
        port=connection["OPENLAKEFORGE_QUERY_TRINO_PORT"],
        catalog=connection["OPENLAKEFORGE_QUERY_TRINO_CATALOG"],
    )
    return StageReportTarget(
        stage=resolved_stage,
        namespace=namespace,
        sqlalchemy_uri=sqlalchemy_uri,
        schema_prefix=environ.get("OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX", ""),
    )


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
    source_dir: Path, bundle_path: Path, bundle_root: str, sqlalchemy_uri: str, *, schema_prefix: str = ""
) -> None:
    """Zip the report YAML, rewriting the database sqlalchemy_uri and dataset schemas in place.

    AWS Glue's shared default catalog (no per-stage custom catalog available)
    needs every physical schema prefixed with the stage's own catalog_name to
    stay collision-free (olf.contracts.build_contract_env's namespace_prefix).
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
            else:
                schema = document.get("schema")
                if not isinstance(schema, str) or not schema:
                    raise ValueError(f"{relative}: has no schema to prefix")
                document["schema"] = f"{schema_prefix}{schema}"
            bundle.writestr(archive_name, yaml.safe_dump(document, sort_keys=False))


def unpack_export_bundle(bundle_path: Path, target_dir: Path) -> None:
    """Replace managed report assets in the source tree from an export zip."""
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
            destination.write_bytes(bundle.read(member))


def _running_superset_pod(namespace: str) -> str:
    raw = k8s._kubectl(  # noqa: SLF001 - internal helper reuse
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app=superset,release=superset",
            "-o",
            'jsonpath={range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\\n"}{end}',
        ],
        capture=True,
    )
    pod = next((line for line in raw.splitlines() if line), "")
    if not pod:
        raise RuntimeError("could not find a running Superset web pod.")
    return pod


def discover_report_dirs(repo_root: Path) -> list[str]:
    root = repo_root / "lakehouse_code" / "dashboards" / "superset"
    dirs = {str(path.parent.relative_to(repo_root)) for path in root.glob("*/metadata.yaml")}
    return sorted(dirs)


def validate_report_registry(repo_root: Path, declared_report_dirs: Sequence[str]) -> list[str]:
    """Require exact parity between descriptor-declared and mounted bundles."""
    declared = set(declared_report_dirs)
    discovered = set(discover_report_dirs(repo_root))
    if declared != discovered:
        missing = sorted(declared - discovered)
        undeclared = sorted(discovered - declared)
        details: list[str] = []
        if missing:
            details.append("declared but not mounted: " + ", ".join(missing))
        if undeclared:
            details.append("mounted but not declared: " + ", ".join(undeclared))
        raise RuntimeError("Superset dashboard registry mismatch (" + "; ".join(details) + ")")
    return sorted(declared)


# The promotion contract for a source-controlled bundle (#130). Superset
# matches assets across instances by `uuid`, so a bundle whose identities are
# missing, duplicated, or dangling imports into DEV and then silently forks or
# fails in PROD -- the one stage nothing re-exports from. These directories are
# what `unpack_export_bundle` manages and `build_report_bundle` packs.
_MANAGED_ASSET_DIRS: tuple[str, ...] = ("databases", "datasets", "charts", "dashboards")
_ASSET_REFERENCES = {"datasets": ("database_uuid", "databases"), "charts": ("dataset_uuid", "datasets")}
# The database is the one identity several bundles are meant to hold in
# common: every dashboard reads the same Gold through one Trino connection,
# and the reference project's bundles all declare the same
# `openlakeforge_trino` uuid deliberately. Nothing else is shareable --
# `deploy_reports` imports every declared bundle into one Superset instance in
# turn, so a chart, dataset, or dashboard uuid reused across two bundles makes
# the second import overwrite the first asset and leaves the earlier dashboard
# pointing at someone else's chart.
_SHAREABLE_ASSET_DIRS = frozenset({"databases"})
# A promotable bundle is stage-neutral: target connectivity is resolved at
# import time by `build_report_bundle`, so a checked-in stage-bound physical
# name (`lakehouse_<stage>` catalogs, `olf-<stage>` namespaces) would survive
# promotion and point PROD at another stage's data. `ws_<user>_` is a personal
# workspace identity, which #111 excludes from promotable dependencies
# outright. `s3://`, `http://`, and credential literals are already rejected
# for every revision component by olf.project_revision.
_FORBIDDEN_BUNDLE_VALUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bws_[a-z0-9][a-z0-9-]*_", re.IGNORECASE), "a personal workspace identifier"),
    (
        # `\b` after the stage token would be wrong on exactly the case that
        # matters most: `_` is a word character, so `lakehouse_dev` matches
        # while AWS's prefixed schema `lakehouse_dev_order_revenue_gold` --
        # what `build_report_bundle`'s `schema_prefix` produces -- would not.
        # A negative lookahead on letters and digits admits the suffix while
        # still refusing `lakehouse_development`.
        re.compile(
            r"(?<![a-z0-9])(?:lakehouse|olf)[_-](?:"
            + "|".join(stage.value for stage in StageName)
            + r")(?![a-z0-9])",
            re.IGNORECASE,
        ),
        "a stage-bound physical name",
    ),
)


def _canonical_uuid(value: object) -> str | None:
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


def _yaml_problem(exc: Exception) -> str:
    """A YAML parse error as one line, for a violation a user reads in a list."""
    return " ".join(str(exc).split())


_UNCOMPARED_DATABASE_FIELDS = frozenset(
    {
        # `build_report_bundle` rewrites it to the target stage's own
        # connection while packaging, so the checked-in value is a
        # placeholder rather than part of the shared definition.
        "sqlalchemy_uri",
        # The canonical identity is already this comparison's map key, so the
        # raw spelling adds nothing -- and comparing it would report two
        # identical databases as conflicting whenever they merely differ in
        # case, which `_canonical_uuid` exists to treat as the same asset.
        "uuid",
    }
)


def _shared_database_definition(document: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a database document two bundles sharing a uuid must agree on."""
    return {key: value for key, value in document.items() if key not in _UNCOMPARED_DATABASE_FIELDS}


def report_bundle_errors(
    repo_root: Path, report_dir: str, *, owner_of: dict[str, tuple[str, str, dict[str, Any]]] | None = None
) -> list[str]:
    """Check one source-controlled report bundle against the promotion contract.

    Returns every violation rather than raising on the first: an analyst
    re-exporting a bundle wants the whole list, not one round trip per
    problem.

    `owner_of` maps each claimed uuid to the (kind, file, document) that
    claimed it. Pass one across several calls to check identity uniqueness
    over a whole set of bundles -- see `validate_report_bundles`.
    """
    bundle_dir = repo_root / report_dir
    metadata_path = bundle_dir / "metadata.yaml"
    if not metadata_path.is_file():
        return [f"{report_dir}/metadata.yaml: missing"]
    # `_IMPORT_SCRIPT` runs `ImportAssetsCommand`, which is what `type:
    # assets` selects -- a dashboard-type export imports through a different
    # command and would fail in the pod rather than here. `_EXPORT_SCRIPT`
    # rewrites the type for exactly that reason, so a bundle that lost it was
    # hand-edited.
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{report_dir}/metadata.yaml: is not valid YAML ({_yaml_problem(exc)})"]
    if not isinstance(metadata, dict):
        return [f"{report_dir}/metadata.yaml: is not a YAML mapping"]
    if metadata.get("type") != "assets":
        return [f"{report_dir}/metadata.yaml: declares type {metadata.get('type')!r}, not 'assets'"]
    if not isinstance(metadata.get("version"), str) or not metadata["version"]:
        return [f"{report_dir}/metadata.yaml: has no export format version"]

    errors: list[str] = []
    owner_of = {} if owner_of is None else owner_of
    identities: dict[str, set[str]] = {kind: set() for kind in _MANAGED_ASSET_DIRS}
    documents: list[tuple[str, str, dict[str, Any]]] = []
    for kind in _MANAGED_ASSET_DIRS:
        for path in sorted((bundle_dir / kind).rglob("*")):
            if not path.is_file() or path.suffix.lower() not in REPORT_YAML_SUFFIXES:
                continue
            name = f"{report_dir}/{path.relative_to(bundle_dir).as_posix()}"
            # A hand-edit or a merge leaves invalid YAML behind often enough
            # that dying on it would make the validator useless exactly when
            # it is needed: the point is to name the file and keep collecting.
            text = path.read_text(encoding="utf-8")
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                errors.append(f"{name}: is not valid YAML ({_yaml_problem(exc)})")
                continue
            if not isinstance(document, dict):
                errors.append(f"{name}: is not a YAML mapping")
                continue
            documents.append((kind, name, document))
            identity = document.get("uuid")
            if not isinstance(identity, str) or not identity:
                errors.append(f"{name}: has no stable uuid")
                continue
            # Superset's import schema expects a real UUID. An arbitrary
            # string matches its own references and would pass every check
            # here, then fail at import into whichever stage runs first.
            canonical = _canonical_uuid(identity)
            if canonical is None:
                errors.append(f"{name}: uuid {identity!r} is not a UUID")
                continue
            identities[kind].add(canonical)
            claimed = owner_of.get(canonical)
            if claimed is None:
                owner_of[canonical] = (kind, name, document)
                continue
            claimed_kind, claimed_name, claimed_document = claimed
            if kind not in _SHAREABLE_ASSET_DIRS or claimed_kind not in _SHAREABLE_ASSET_DIRS:
                errors.append(f"{name}: uuid {canonical} is already used by {claimed_name}")
            elif _shared_database_definition(document) != _shared_database_definition(claimed_document):
                errors.append(
                    f"{name}: uuid {canonical} defines a different database than {claimed_name}; "
                    "a shared connection must be declared identically in every bundle"
                )

    # A bundle carrying only a database and its datasets is what `olf product
    # new --with-report` leaves behind: the scaffold cannot invent a dashboard
    # layout, so the analyst authors one in Superset and exports it back. That
    # state is legitimate on disk and not promotable, and `e2e._assertions`
    # already refuses it at runtime -- accepting it here would publish an
    # incomplete revision and surface the omission only after promotion.
    if not any(kind == "dashboards" for kind, _, _ in documents):
        errors.append(
            f"{report_dir}: exports no Superset dashboard; author it in Superset "
            "and export it back into this bundle before promoting"
        )

    for kind, name, document in documents:
        if kind in _ASSET_REFERENCES:
            field, target = _ASSET_REFERENCES[kind]
            reference = document.get(field)
            if _canonical_uuid(reference) not in identities[target]:
                errors.append(f"{name}: {field} {reference!r} does not resolve inside the bundle")
            continue
        if kind != "dashboards":
            continue
        position = document.get("position")
        for key, block in position.items() if isinstance(position, dict) else ():
            if not isinstance(block, dict) or block.get("type") != "CHART":
                continue
            meta = block.get("meta")
            reference = meta.get("uuid") if isinstance(meta, dict) else None
            if _canonical_uuid(reference) not in identities["charts"]:
                errors.append(f"{name}: position block {key} references chart {reference!r} the bundle does not define")

    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in REPORT_YAML_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        name = f"{report_dir}/{path.relative_to(bundle_dir).as_posix()}"
        for pattern, detail in _FORBIDDEN_BUNDLE_VALUES:
            match = pattern.search(text)
            if match is not None:
                errors.append(f"{name}: contains {detail} ({match.group(0)!r})")
    return errors


def validate_report_bundles(repo_root: Path, report_dirs: Sequence[str]) -> list[str]:
    """Collect promotion-contract violations across every declared bundle.

    Identity uniqueness is checked over the whole set rather than one bundle
    at a time, because the hazard is per Superset instance rather than per
    bundle: `deploy_reports` imports them all into the same one.
    """
    owner_of: dict[str, tuple[str, str, dict[str, Any]]] = {}
    return [
        error for report_dir in report_dirs for error in report_bundle_errors(repo_root, report_dir, owner_of=owner_of)
    ]


def _exec_pod_python(pod: str, namespace: str, script: str, args: list[str]) -> None:
    quoted = " ".join(f"'{arg}'" for arg in args)
    command = f". /app/pythonpath/superset_bootstrap.sh; python - {quoted}"
    subprocess.run(
        k8s._resolved_kubectl_argv(  # noqa: SLF001 - internal helper reuse
            ["exec", "-i", pod, "-c", "superset", "-n", namespace, "--", "/bin/sh", "-ec", command]
        ),
        input=script,
        text=True,
        check=True,
    )


def deploy_reports(
    repo_root: Path,
    namespace: str,
    sqlalchemy_uri: str,
    *,
    report_source_dir: str | None,
    declared_report_dirs: Sequence[str] | None = None,
    work_dir: Path,
    reports_mount_path: str,
    admin_username: str,
    schema_prefix: str = "",
) -> None:
    # Computed before touching the cluster: a declared-but-unmounted bundle
    # must still fail loudly, and validate_report_registry already does that
    # (raises before returning) whenever declared_report_dirs is given, so
    # reaching the empty check below means every declared dashboard really
    # is missing -- i.e. none are declared at all (#228's scaffolded-but-
    # undeclared draft, or any project that simply has no dashboards yet).
    # That is a legitimate state, not a misconfiguration: importing nothing
    # is the correct outcome, not an error to wait for a live Superset pod
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

    log.step("Waiting for Superset web deployment...")
    k8s.wait_for_rollout("deployment/superset", namespace)
    pod = _running_superset_pod(namespace)

    work_dir.mkdir(parents=True, exist_ok=True)
    for report_dir in report_dirs:
        source_dir = repo_root / report_dir
        if not (source_dir / "metadata.yaml").is_file():
            raise RuntimeError(f"missing Superset report metadata at {report_dir}/metadata.yaml")

        identity = bundle_identity(report_dir)
        bundle_path = work_dir / identity.name
        remote_bundle = f"{reports_mount_path}/{identity.name}"
        build_report_bundle(source_dir, bundle_path, identity.root, sqlalchemy_uri, schema_prefix=schema_prefix)

        log.step(f"Copying {bundle_path} to {pod}:{remote_bundle}")
        with bundle_path.open("rb") as body:
            subprocess.run(
                k8s._resolved_kubectl_argv(  # noqa: SLF001 - internal helper reuse
                    [
                        "exec",
                        "-i",
                        pod,
                        "-c",
                        "superset",
                        "-n",
                        namespace,
                        "--",
                        "/bin/sh",
                        "-ec",
                        f"mkdir -p '{reports_mount_path}' && cat > '{remote_bundle}'",
                    ]
                ),
                stdin=body,
                check=True,
            )

        log.step(f"Importing Superset report assets from {remote_bundle}")
        _exec_pod_python(pod, namespace, _IMPORT_SCRIPT, [remote_bundle, admin_username])
        log.info(f"Deployed Superset report assets from {report_dir}")


def export_report(
    repo_root: Path,
    namespace: str,
    *,
    report_source_dir: str,
    bundle_name: str,
    work_dir: Path,
    reports_mount_path: str,
    admin_username: str,
    dashboard_title: str,
) -> None:
    identity = bundle_identity(report_source_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    local_bundle = work_dir / bundle_name
    remote_bundle = f"{reports_mount_path}/{bundle_name}"

    log.step("Waiting for Superset web deployment...")
    k8s.wait_for_rollout("deployment/superset", namespace)
    pod = _running_superset_pod(namespace)

    log.step(f"Exporting '{dashboard_title}' from Superset")
    _exec_pod_python(pod, namespace, _EXPORT_SCRIPT, [remote_bundle, admin_username, dashboard_title, identity.root])

    with local_bundle.open("wb") as out:
        subprocess.run(
            k8s._resolved_kubectl_argv(  # noqa: SLF001 - internal helper reuse
                ["exec", pod, "-c", "superset", "-n", namespace, "--", "cat", remote_bundle]
            ),
            stdout=out,
            check=True,
        )

    unpack_export_bundle(local_bundle, repo_root / report_source_dir)
    log.info(f"Exported Superset report assets to {report_source_dir}")
