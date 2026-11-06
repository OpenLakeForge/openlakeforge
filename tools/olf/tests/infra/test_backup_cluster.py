"""`olf infra backup`'s cluster bindings that decide what reaches disk (#910)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.infra.backup import BackupError
from olf.infra.backup._cluster import _copy_bucket


class FakeS3:
    def __init__(self, objects: dict[str, bytes], *, listed_size: dict[str, int] | None = None) -> None:
        self.objects = objects
        self.listed_size = listed_size or {}

    def get_paginator(self, _: str) -> Any:
        listing = [{"Key": key, "Size": self.listed_size.get(key, len(body))} for key, body in self.objects.items()]
        return type("Paginator", (), {"paginate": lambda _self, Bucket: [{"Contents": listing}]})()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[key])


def test_every_object_lands_under_its_key_and_a_folder_marker_is_not_one(tmp_path: Path) -> None:
    client = FakeS3({"iceberg/t/a.parquet": b"abc", "iceberg/": b"", "b.json": b"{}"})

    inventory = _copy_bucket(client, "lakehouse-dev-gold", tmp_path / "lakehouse-dev-gold")

    assert inventory == {"iceberg/t/a.parquet": 3, "b.json": 2}
    assert (tmp_path / "lakehouse-dev-gold" / "iceberg" / "t" / "a.parquet").read_bytes() == b"abc"


def test_a_key_that_climbs_out_of_the_backup_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="outside the backup"):
        _copy_bucket(FakeS3({"../escape": b"x"}), "b", tmp_path / "b")

    assert not (tmp_path / "escape").exists()


def test_keys_that_resolve_to_the_same_file_are_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="map to the same backup file"):
        _copy_bucket(FakeS3({"a/b": b"one", "a//b": b"two"}), "b", tmp_path / "b")


def test_a_download_that_is_not_the_listed_size_fails(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="is 3 bytes, not 9"):
        _copy_bucket(FakeS3({"a": b"abc"}, listed_size={"a": 9}), "b", tmp_path / "b")


def test_the_command_is_registered_and_has_no_passphrase_option() -> None:
    result = CliRunner().invoke(app, ["infra", "backup", "--help"])

    assert result.exit_code == 0
    assert "--output" in result.output and "OLF_BACKUP_PASSPHRASE" in result.output
    assert "--passphrase" not in result.output
