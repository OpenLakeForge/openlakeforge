"""Extracting a verified archive into an active, executable managed tool.

`install` never lets a partial or unverified artifact become the active
binary: it stages into a process-unique directory, sets the executable bit,
then `os.replace`s onto the final path — an atomic rename on the same
filesystem, so a reader either sees the previous binary or the new one, never
a half-written file.
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

from olf.toolchain.spec import ToolSpec, activated_filename


def install(archive_path: Path, spec: ToolSpec, *, bin_dir: Path) -> Path:
    """Extract `archive_path` (already digest-verified) and activate `spec`'s
    executable at `bin_dir/<name>-<digest>` (see `activated_filename`) -
    content-addressed, not a plain `<name>`, so two different pins can never
    share one mutable path. Returns the activated path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(dir=bin_dir, prefix=f".staging-{spec.name}-"))
    try:
        extracted = _extract(archive_path, spec, staging_dir=staging_dir)
        extracted.chmod(extracted.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        target = bin_dir / activated_filename(spec.name, spec.sha256)
        os.replace(extracted, target)
        return target
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _extract(archive_path: Path, spec: ToolSpec, *, staging_dir: Path) -> Path:
    if spec.archive == "raw":
        destination = staging_dir / spec.name
        shutil.copyfile(archive_path, destination)
        return destination

    if spec.archive == "zip":
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(staging_dir)  # noqa: S202 - archive_path is a digest-verified upstream release
    elif spec.archive == "tar.gz":
        with tarfile.open(archive_path, mode="r:gz") as archive:
            archive.extractall(staging_dir, filter="data")  # noqa: S202 - see above
    else:
        raise ValueError(f"unsupported archive kind: {spec.archive!r}")

    if spec.member is None:
        raise ValueError(f"{spec.name}: archive kind {spec.archive!r} requires an explicit member path")
    member_path = staging_dir / spec.member
    if not member_path.is_file():
        raise ValueError(f"{spec.name}: archive did not contain expected member {spec.member!r}")
    return member_path
