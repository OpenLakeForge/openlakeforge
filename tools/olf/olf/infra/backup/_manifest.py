"""The backup directory's manifest and checksums.

The manifest names what the backup holds: databases, buckets and the Secrets in `secrets.enc`, by name. It never
carries a Secret value. `SHA256SUMS` covers every other file in the directory, `manifest.json` included, so
`verify_checksums` is what tells `restore` the directory is the one `backup` wrote.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from olf.infra.backup import BackupError

FORMAT = 1
MANIFEST_FILE = "manifest.json"
CHECKSUMS_FILE = "SHA256SUMS"
_CHUNK = 1 << 20


@dataclass(frozen=True)
class DatabaseDump:
    name: str
    owner: str
    file: str  # relative to the backup directory: postgres/<name>.dump
    sha256: str
    bytes: int


@dataclass(frozen=True)
class BucketCopy:
    name: str
    objects: int
    bytes: int
    inventory_file: str  # relative to the backup directory: objects/<name>.inventory.json


@dataclass(frozen=True)
class SecretRef:
    namespace: str
    name: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    format: int
    release: str
    profile_sha256: str
    provider: str
    stages: tuple[str, ...]
    databases: tuple[DatabaseDump, ...]
    buckets: tuple[BucketCopy, ...]
    secrets: tuple[SecretRef, ...]
    dagster_paused: bool
    started_at: str
    finished_at: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _names(directory: Path) -> list[str]:
    names = (path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file())
    return sorted(name for name in names if name != CHECKSUMS_FILE)


def write_manifest(directory: Path, manifest: Manifest) -> None:
    """Write `manifest.json`, then `SHA256SUMS` over every file in `directory` except itself."""
    (directory / MANIFEST_FILE).write_text(json.dumps(asdict(manifest), indent=1, sort_keys=True) + "\n")
    lines = (f"{sha256_file(directory / name)}  {name}\n" for name in _names(directory))
    (directory / CHECKSUMS_FILE).write_text("".join(lines))


def load_manifest(directory: Path) -> Manifest:
    try:
        raw = json.loads((directory / MANIFEST_FILE).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"cannot read {directory / MANIFEST_FILE}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise BackupError(f"{directory / MANIFEST_FILE} is not a format {FORMAT} backup manifest.")
    try:
        return Manifest(
            **{
                **raw,
                "stages": tuple(raw["stages"]),
                "databases": tuple(DatabaseDump(**item) for item in raw["databases"]),
                "buckets": tuple(BucketCopy(**item) for item in raw["buckets"]),
                "secrets": tuple(SecretRef(**{**item, "keys": tuple(item["keys"])}) for item in raw["secrets"]),
            }
        )
    except (KeyError, TypeError) as exc:
        raise BackupError(f"{directory / MANIFEST_FILE} is malformed: {exc!r}") from exc


def verify_checksums(directory: Path) -> list[str]:
    """What differs between `SHA256SUMS` and the directory: a changed, missing or unlisted file. Empty when clean."""
    try:
        listed = (directory / CHECKSUMS_FILE).read_text().splitlines()
    except OSError:
        return [f"{CHECKSUMS_FILE} is missing"]
    expected = {name: digest for digest, _, name in (line.partition("  ") for line in listed)}
    problems = []
    for name, digest in expected.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name} is missing")
        elif sha256_file(path) != digest:
            problems.append(f"{name} does not match its checksum")
    problems += [f"{name} is not in {CHECKSUMS_FILE}" for name in _names(directory) if name not in expected]
    return problems
