"""`olf bucket`: the files in a stage's lakehouse buckets, through the platform API (ADR 0021).

The API decides which buckets and stages the signed-in person may touch, so these commands name a bucket and a
key and nothing else: no object-store endpoint or key is configured here. Keys travel as query parameters or JSON,
never in the URL path, because a key contains `/`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import typer

from olf.common.log import fail
from olf.platform import api
from olf.platform import session as sessions
from olf.platform.output import table
from olf.platform.reads import JSON_OPTION, PROFILE_OPTION, STAGE_OPTION, _read, _show, stage_path

bucket_app = typer.Typer(help="Files in a stage's lakehouse buckets, through the platform API.", no_args_is_help=True)


def _split(target: str) -> tuple[str, str]:
    """`bucket/some/key` as ("bucket", "some/key"); the bucket is the first segment, the rest keeps its slashes."""
    bucket, _, key = target.lstrip("/").partition("/")
    if not bucket:
        raise typer.Exit(code=fail("name a bucket, as <bucket>/<key>"))
    return bucket, key


def _object_path(stage: str, bucket: str, rest: str) -> str:
    return stage_path(stage, f"buckets/{quote(bucket, safe='')}/{rest}")


def _call(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{count} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


@bucket_app.command("list")
def bucket_list(stage: str = STAGE_OPTION, as_json: bool = JSON_OPTION, profile_file: str = PROFILE_OPTION) -> None:
    """List the buckets of a stage you may browse."""
    buckets = _read(profile_file, stage_path(stage, "buckets"))["buckets"]
    _show(
        as_json,
        buckets,
        ["bucket", "created"],
        lambda items: [[item["name"], item["created_at"] or "-"] for item in items],
    )


@bucket_app.command("ls")
def bucket_ls(
    target: str = typer.Argument(..., help="<bucket> or <bucket>/<prefix>/ to list."),
    stage: str = STAGE_OPTION,
    as_json: bool = JSON_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """List the folders and files directly under a bucket or prefix."""
    bucket, prefix = _split(target)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    folders: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params = {"prefix": prefix, **({"cursor": cursor} if cursor else {})}
        page = _read(profile_file, _object_path(stage, bucket, "objects"), params)
        folders += page["folders"]
        files += page["files"]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    _show(
        as_json,
        {"prefix": prefix, "folders": folders, "files": files},
        ["name", "size", "modified"],
        lambda data: [
            *([item["prefix"], "-", "-"] for item in data["folders"]),
            *([item["key"], _size(item["size"]), item["last_modified"]] for item in data["files"]),
        ],
    )


@bucket_app.command("get")
def bucket_get(
    target: str = typer.Argument(..., help="<bucket>/<key> to download."),
    dest: Annotated[Path | None, typer.Argument(help="Where to write it; default ./<name of the key>.")] = None,
    stage: str = STAGE_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Download one file, streamed to disk."""
    bucket, key = _split(target)
    name = key.rsplit("/", 1)[-1]
    if not name:
        raise typer.Exit(code=fail("name a file, as <bucket>/<key>; a folder cannot be downloaded"))
    path = Path(name) if dest is None else dest / name if dest.is_dir() else dest
    written = _call(
        lambda: api.get_to_path(
            sessions.resolve_instance(profile_file), _object_path(stage, bucket, "objects/content"), {"key": key}, path
        )
    )
    typer.echo(f"{bucket}/{key} -> {path} ({_size(written)})")


@bucket_app.command("put")
def bucket_put(
    src: Annotated[Path, typer.Argument(help="Local file to upload.")],
    target: str = typer.Argument(..., help="<bucket>/<key>, or <bucket>/<prefix>/ to keep the file's name."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace the object when it exists."),
    stage: str = STAGE_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Upload one file, streamed from disk."""
    bucket, key = _split(target)
    if not src.is_file():
        raise typer.Exit(code=fail(f"{src} is not a file"))
    if not key or key.endswith("/"):
        key += src.name
    size = src.stat().st_size

    def upload() -> Any:
        with src.open("rb") as source:
            return api.put_stream(
                sessions.resolve_instance(profile_file),
                _object_path(stage, bucket, "objects/content"),
                {"key": key, "overwrite": "true" if overwrite else "false"},
                source,
                size,
            )

    done = _call(upload)
    typer.echo(f"{src} -> {bucket}/{done['key']} ({_size(done['size'])})")


def _post(profile_file: str, stage: str, bucket: str, rest: str, body: dict[str, Any]) -> Any:
    return _call(lambda: api.post(sessions.resolve_instance(profile_file), _object_path(stage, bucket, rest), body))


@bucket_app.command("mkdir")
def bucket_mkdir(
    target: str = typer.Argument(..., help="<bucket>/<prefix>/ to create."),
    stage: str = STAGE_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Create a folder."""
    bucket, prefix = _split(target)
    if not prefix:
        raise typer.Exit(code=fail("name a folder, as <bucket>/<prefix>/"))
    folder = prefix if prefix.endswith("/") else prefix + "/"
    created = _post(profile_file, stage, bucket, "folders", {"prefix": folder})
    typer.echo(f"created {bucket}/{created['prefix']}")


@bucket_app.command("mv")
def bucket_mv(
    source: str = typer.Argument(..., help="<bucket>/<key>, or <bucket>/<prefix>/ for a folder."),
    to: str = typer.Argument(..., help="New key in the same bucket; ends in / exactly when the source does."),
    stage: str = STAGE_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Rename or move a file or folder within its bucket."""
    bucket, old = _split(source)
    new = to.lstrip("/")
    if not old or not new:
        raise typer.Exit(code=fail("name a source key and a destination key"))
    if old.endswith("/") != new.endswith("/"):
        raise typer.Exit(code=fail("a folder moves to a folder: both keys end in '/', or neither does"))
    moved = _post(profile_file, stage, bucket, "objects/rename", {"from": old, "to": new})
    typer.echo(f"moved {moved['moved']} object(s): {bucket}/{old} -> {bucket}/{new}")


@bucket_app.command("rm")
def bucket_rm(
    target: str = typer.Argument(..., help="<bucket>/<key>, or <bucket>/<prefix>/ to delete everything under it."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without asking."),
    stage: str = STAGE_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Delete a file, or a whole folder when the key ends in /."""
    bucket, key = _split(target)
    if not key:
        raise typer.Exit(code=fail("name a key or prefix to delete, as <bucket>/<key>"))
    if not yes:
        what = f"everything under {bucket}/{key}" if key.endswith("/") else f"{bucket}/{key}"
        typer.confirm(f"Delete {what}? This cannot be undone.", abort=True)
    result = _post(profile_file, stage, bucket, "objects/delete", {"keys": [key]})
    typer.echo(f"deleted {len(result['deleted'])} key(s)")
    if result["errors"]:
        rows = [[item["key"], item["detail"]] for item in result["errors"]]
        typer.echo(table(["key", "error"], rows), err=True, nl=False)
        raise typer.Exit(code=1)
