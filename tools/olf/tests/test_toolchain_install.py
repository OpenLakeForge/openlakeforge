from __future__ import annotations

import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from olf.toolchain.install import install
from olf.toolchain.platform import Platform
from olf.toolchain.spec import ToolSpec, activated_filename

_PLATFORM = Platform(os="linux", arch="amd64")


def _spec(**overrides: object) -> ToolSpec:
    defaults = dict(
        name="tool",
        version="1.0.0",
        platform=_PLATFORM,
        sha256="sha256:" + "a" * 64,
        url="https://example.invalid/tool",
        archive="raw",
        member=None,
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)  # type: ignore[arg-type]


def test_install_activates_a_raw_binary(tmp_path: Path) -> None:
    archive = tmp_path / "downloaded"
    archive.write_bytes(b"#!/bin/sh\necho hi\n")
    spec = _spec(name="kind", archive="raw")

    activated = install(archive, spec, bin_dir=tmp_path / "bin")

    assert activated == tmp_path / "bin" / activated_filename("kind", spec.sha256)
    assert activated.read_bytes() == b"#!/bin/sh\necho hi\n"
    assert activated.stat().st_mode & stat.S_IXUSR


def test_install_extracts_a_zip_member(tmp_path: Path) -> None:
    archive = tmp_path / "downloaded.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("terraform", b"pretend terraform binary")
        zf.writestr("LICENSE.txt", b"license text")
    spec = _spec(name="terraform", archive="zip", member="terraform")

    activated = install(archive, spec, bin_dir=tmp_path / "bin")

    assert activated == tmp_path / "bin" / activated_filename("terraform", spec.sha256)
    assert activated.read_bytes() == b"pretend terraform binary"


def test_install_extracts_a_nested_targz_member(tmp_path: Path) -> None:
    archive = tmp_path / "downloaded.tar.gz"
    inner_dir = tmp_path / "helm-src"
    inner_dir.mkdir()
    (inner_dir / "helm").write_bytes(b"pretend helm binary")
    (inner_dir / "README.md").write_text("readme")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(inner_dir, arcname="linux-amd64")
    spec = _spec(name="helm", archive="tar.gz", member="linux-amd64/helm")

    activated = install(archive, spec, bin_dir=tmp_path / "bin")

    assert activated.read_bytes() == b"pretend helm binary"


def test_install_raises_when_the_expected_member_is_absent(tmp_path: Path) -> None:
    archive = tmp_path / "downloaded.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("unexpected-name", b"data")
    spec = _spec(name="terraform", archive="zip", member="terraform")

    with pytest.raises(ValueError, match="did not contain"):
        install(archive, spec, bin_dir=tmp_path / "bin")


def test_install_leaves_no_staging_directory_behind_on_success(tmp_path: Path) -> None:
    archive = tmp_path / "downloaded"
    archive.write_bytes(b"binary")
    bin_dir = tmp_path / "bin"
    spec = _spec(name="kind")

    install(archive, spec, bin_dir=bin_dir)

    remaining = list(bin_dir.iterdir())
    assert remaining == [bin_dir / activated_filename("kind", spec.sha256)]


def test_install_leaves_no_staging_directory_or_active_binary_on_failure(tmp_path: Path) -> None:
    archive = tmp_path / "downloaded.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("unexpected-name", b"data")
    bin_dir = tmp_path / "bin"
    spec = _spec(name="terraform", archive="zip", member="terraform")

    with pytest.raises(ValueError):
        install(archive, spec, bin_dir=bin_dir)

    assert not (bin_dir / "terraform").exists()
    assert list(bin_dir.iterdir()) == []


def test_install_never_touches_a_different_digest_of_the_same_tool(tmp_path: Path) -> None:
    """Two different pins of the same tool must activate at two distinct,
    immutable paths - never share one mutable `<tool>` path a later install
    could swap out from under an in-flight resolution of the other pin."""
    bin_dir = tmp_path / "bin"
    old_spec = _spec(name="kind", sha256="sha256:" + "a" * 64)
    old_archive = tmp_path / "old"
    old_archive.write_bytes(b"old version")
    old_activated = install(old_archive, old_spec, bin_dir=bin_dir)

    new_spec = _spec(name="kind", sha256="sha256:" + "b" * 64)
    new_archive = tmp_path / "new"
    new_archive.write_bytes(b"new version")
    new_activated = install(new_archive, new_spec, bin_dir=bin_dir)

    assert old_activated != new_activated
    assert old_activated.read_bytes() == b"old version"
    assert new_activated.read_bytes() == b"new version"


def test_install_of_the_same_digest_twice_is_idempotent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    spec = _spec(name="kind")
    archive = tmp_path / "downloaded"
    archive.write_bytes(b"same content")

    first = install(archive, spec, bin_dir=bin_dir)
    second = install(archive, spec, bin_dir=bin_dir)

    assert first == second
    assert second.read_bytes() == b"same content"
