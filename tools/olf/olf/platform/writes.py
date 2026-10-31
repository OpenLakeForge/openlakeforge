"""`olf runs launch` and `olf report import|export`: calls to the platform API that change state, as the person.

The API decides whether the person may act on the stage and records the attempt
before it acts (ADR 0021, decision 3), so each command sends its request once and
never retries it. `olf report export` changes nothing remote; it sits here beside
the import it pairs with.
"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

import typer
from openlakeforge_services import reports

from olf.common.log import fail
from olf.platform import api
from olf.platform import session as sessions
from olf.platform.reads import JSON_OPTION, PROFILE_OPTION, STAGE_OPTION, stage_path


def runs_launch(
    job: str = typer.Argument(..., help="The job to launch, as `olf jobs list` prints it."),
    stage: str = STAGE_OPTION,
    as_json: bool = JSON_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Launch one of a stage's jobs as you; the API records who asked for it."""
    try:
        run = api.post(sessions.resolve_instance(profile_file), stage_path(stage, "runs"), {"job": job})
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if as_json:
        typer.echo(json.dumps(run, indent=2))
        return
    typer.echo(f"Launched {run['job']} on {run['stage']}: run {run['run_id']}")
    typer.echo(f"Follow it with `olf runs show {run['run_id']} --stage {run['stage']}`.")


# The platform API refuses a larger upload (packages/platform-api, report_routes.MAX_UPLOAD_BYTES); checking here
# says so before sending it.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_KINDS = ("datasets", "charts", "dashboards")
_IMPORT_DIR = typer.Argument(..., help="The report directory, as `olf report export` writes it.")
_EXPORT_DIR = typer.Argument(..., help="The report directory to write; its managed assets are replaced.")


def zip_report(report_dir: Path) -> bytes:
    """metadata.yaml and the YAML under datasets/, charts/ and dashboards/, zipped from the report's root.

    That is all the API imports; databases/ stays behind. The limits are the ones the API holds an upload to.
    """
    files = [report_dir / "metadata.yaml"] + sorted(
        path
        for kind in UPLOAD_KINDS
        for path in (report_dir / kind).rglob("*")
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in {".yaml", ".yml"}
    )
    if len(files) > reports.MAX_UPLOAD_MEMBERS:
        raise sessions.PlatformError(f"{report_dir} has more than {reports.MAX_UPLOAD_MEMBERS} YAML files")
    sizes = {path: path.stat().st_size for path in files}
    if big := [path for path, size in sizes.items() if size > reports.MAX_UPLOAD_MEMBER_BYTES]:
        raise sessions.PlatformError(f"{big[0]} is over {reports.MAX_UPLOAD_MEMBER_BYTES} bytes")
    if sum(sizes.values()) > reports.MAX_UPLOAD_TOTAL_BYTES:
        raise sessions.PlatformError(f"{report_dir} holds over {reports.MAX_UPLOAD_TOTAL_BYTES} bytes of YAML")
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(report_dir).as_posix())
    if out.tell() > MAX_UPLOAD_BYTES:
        raise sessions.PlatformError(f"{report_dir} zips to over {MAX_UPLOAD_BYTES} bytes")
    return out.getvalue()


def report_import(
    report_dir: Path = _IMPORT_DIR,
    stage: str = STAGE_OPTION,
    as_json: bool = JSON_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Import a report into a stage's Superset as you; DEV only. Its databases are never sent."""
    try:
        if not (report_dir / "metadata.yaml").is_file():
            raise sessions.PlatformError(f"{report_dir} has no metadata.yaml; it is not a report directory")
        result = api.post_bytes(
            sessions.resolve_instance(profile_file),
            stage_path(stage, "reports"),
            zip_report(report_dir),
            "application/zip",
        )
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"Imported {len(result['dashboards'])} dashboard(s) into {result['stage']}'s Superset.")


def report_export(
    dashboard: str = typer.Argument(..., help="The dashboard's uuid or slug."),
    report_dir: Path = _EXPORT_DIR,
    stage: str = STAGE_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Export a dashboard from a stage's Superset as a stage-neutral report directory."""
    try:
        bundle = api.get_bytes(
            sessions.resolve_instance(profile_file),
            stage_path(stage, f"reports/{quote(dashboard, safe='')}/export"),
        )
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    # The API's bundle is already stage-neutral; the shared unpacker replaces the managed assets in place.
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "export.zip"
        path.write_bytes(bundle)
        reports.unpack_export_bundle(path, report_dir)
    typer.echo(f"Exported {dashboard} from {stage} into {report_dir}.")
