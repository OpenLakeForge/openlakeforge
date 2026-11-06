"""The backup manifest and SHA256SUMS (#910)."""

from __future__ import annotations

from pathlib import Path

import pytest

from olf.infra.backup import BackupError
from olf.infra.backup._manifest import (
    FORMAT,
    BucketCopy,
    DatabaseDump,
    Manifest,
    SecretRef,
    load_manifest,
    sha256_file,
    verify_checksums,
    write_manifest,
)


def _directory(tmp_path: Path) -> tuple[Path, Manifest]:
    (tmp_path / "postgres").mkdir()
    dump = tmp_path / "postgres" / "polaris.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / "objects").mkdir()
    (tmp_path / "objects" / "lakehouse-dev-gold.inventory.json").write_text('{"a.parquet": 3}')
    (tmp_path / "secrets.enc").write_bytes(b"sealed")
    manifest = Manifest(
        format=FORMAT,
        release="0.6.0",
        profile_sha256="ab" * 32,
        provider="local",
        stages=("dev", "prod"),
        databases=(DatabaseDump("polaris", "polaris", "postgres/polaris.dump", sha256_file(dump), 5),),
        buckets=(BucketCopy("lakehouse-dev-gold", 1, 3, "objects/lakehouse-dev-gold.inventory.json"),),
        secrets=(SecretRef("olf-system", "polaris-bootstrap-credentials", ("ROOT_CLIENT_ID", "ROOT_CLIENT_SECRET")),),
        dagster_paused=True,
        started_at="2026-11-06T10:00:00Z",
        finished_at="2026-11-06T10:05:00Z",
    )
    return tmp_path, manifest


def test_a_manifest_round_trips_and_its_checksums_are_clean(tmp_path: Path) -> None:
    directory, manifest = _directory(tmp_path)

    write_manifest(directory, manifest)

    assert load_manifest(directory) == manifest
    assert verify_checksums(directory) == []
    sums = (directory / "SHA256SUMS").read_text()
    assert "postgres/polaris.dump" in sums and "manifest.json" in sums and "SHA256SUMS" not in sums


def test_a_tampered_missing_and_unlisted_file_are_each_reported(tmp_path: Path) -> None:
    directory, manifest = _directory(tmp_path)
    write_manifest(directory, manifest)

    (directory / "postgres" / "polaris.dump").write_bytes(b"changed")
    (directory / "secrets.enc").unlink()
    (directory / "extra.txt").write_text("x")

    assert verify_checksums(directory) == [
        "postgres/polaris.dump does not match its checksum",
        "secrets.enc is missing",
        "extra.txt is not in SHA256SUMS",
    ]


def test_a_directory_without_sums_is_reported(tmp_path: Path) -> None:
    assert verify_checksums(tmp_path) == ["SHA256SUMS is missing"]


def test_another_manifest_format_is_refused(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text('{"format": 2}')

    with pytest.raises(BackupError, match="not a format 1"):
        load_manifest(tmp_path)
