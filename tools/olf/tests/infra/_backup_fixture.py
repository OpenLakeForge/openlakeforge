"""A backup directory in the format `olf infra backup` writes, shared by the restore tests (#911)."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from olf.infra.backup._bundle import encrypt_secrets
from olf.infra.backup._manifest import (
    FORMAT,
    BucketCopy,
    DatabaseDump,
    Manifest,
    SecretRef,
    sha256_file,
    write_manifest,
)

PASSPHRASE = "correct horse"
PROFILE_TEXT = "apiVersion: openlakeforge.io/v1\n"
SHARED = "olf-system"
STAGES = {"dev": "olf-dev", "prod": "olf-prod"}


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


SECRETS = [
    {"namespace": SHARED, "name": "polaris-bootstrap-credentials", "type": "Opaque", "data": {"password": b64("pw")}},
    {
        "namespace": SHARED,
        "name": "local-ca",
        "type": "kubernetes.io/tls",
        "data": {"tls.crt": b64("CERT"), "tls.key": b64("KEY")},
    },
]


class Backup:
    """A backup directory and the profile it was taken with."""

    def __init__(self, tmp_path: Path) -> None:
        self.directory = tmp_path / "backup"
        self.profile = tmp_path / "openlakeforge.yaml"
        self.profile.write_text(PROFILE_TEXT)
        (self.directory / "postgres").mkdir(parents=True)
        for name in ("dagster_dev", "openmetadata_db", "polaris"):
            (self.directory / "postgres" / f"{name}.dump").write_bytes(f"dump of {name}".encode())
        objects = self.directory / "objects"
        (objects / "lakehouse-dev-silver" / "t").mkdir(parents=True)
        (objects / "lakehouse-dev-silver" / "t" / "a.parquet").write_bytes(b"abcd")
        (objects / "lakehouse-dev-silver" / "b.json").write_bytes(b"xyz")
        (objects / "lakehouse-dev-silver.inventory.json").write_text(json.dumps({"t/a.parquet": 4, "b.json": 3}))
        self.write(bundle=SECRETS)

    def write(self, *, bundle: list[dict[str, Any]] | None = None, **overrides: Any) -> None:
        """(Re)write `secrets.enc` (`bundle`), `manifest.json`, `SHA256SUMS`; `overrides` replace manifest fields."""
        if bundle is not None:
            (self.directory / "secrets.enc").write_bytes(encrypt_secrets(bundle, PASSPHRASE))
        manifest = Manifest(
            format=FORMAT,
            release="1.2.3",
            profile_sha256=sha256_file(self.profile),
            provider="local",
            stages=("dev", "prod"),
            databases=(
                DatabaseDump("dagster_dev", "dagster_dev", "postgres/dagster_dev.dump", "x", 4),
                DatabaseDump("openmetadata_db", "openmetadata_user", "postgres/openmetadata_db.dump", "x", 4),
                DatabaseDump("polaris", "polaris", "postgres/polaris.dump", "x", 4),
            ),
            buckets=(BucketCopy("lakehouse-dev-silver", 2, 7, "objects/lakehouse-dev-silver.inventory.json"),),
            secrets=(
                SecretRef(SHARED, "polaris-bootstrap-credentials", ("password",)),
                SecretRef(SHARED, "local-ca", ("tls.crt", "tls.key")),
            ),
            dagster_paused=True,
            started_at="2026-11-06T00:00:00Z",
            finished_at="2026-11-06T00:05:00Z",
        )
        write_manifest(self.directory, replace(manifest, **overrides))
