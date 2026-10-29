"""A deterministic, scaled copy of a project's CSV Bronze fixtures.

Scaling replicates the whole fixture universe `scale` times. Replica 0 is the
fixture itself; replica `r` suffixes every primary-key value, and every column
named after some entity's primary key, with `-R<r>`. Each replica is therefore
a disjoint copy whose references resolve inside it, and a column that refers
to a key under another name (`orders.customer_id` -> `accounts.account_id`)
keeps pointing into replica 0, which also exists. No randomness: the same
fixtures and scale produce byte-identical files, so Silver row counts are
known exactly before a run.
"""

from __future__ import annotations

import csv
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml
from openlakeforge_domain import inventory_for

from olf.bench._record import BenchError

# The reference project's loaders read `bronze/<source>/examples/<resource>.csv`
# (lakehouse_code/bronze/*/dlt); the descriptors do not name the file.
FIXTURE_DIR = "examples"


@dataclass(frozen=True)
class GeneratedTable:
    source: str
    resource: str
    rows: int
    bytes: int
    sha256: str


def primary_key_columns(lakehouse_root: Path) -> dict[tuple[str, str], str]:
    """`(source, resource) -> primary-key column` for every Floe entity with a single string key."""
    keys: dict[tuple[str, str], str] = {}
    for path in sorted(lakehouse_root.glob("silver/*/contracts/floe/*.yml")):
        if path.name == "_domain.yml":
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        schema = document.get("schema") or {}
        key = schema.get("primary_key") or []
        if len(key) != 1:
            raise BenchError(f"{path}: scaling needs a single-column primary key, found {key!r}")
        types = {column.get("name"): column.get("type") for column in schema.get("columns") or []}
        if types.get(key[0]) != "string":
            raise BenchError(f"{path}: primary key {key[0]!r} must be a string column to be suffixed per replica")
        keys[(document["source"]["ref"], document["source"]["resource"])] = key[0]
    return keys


def generate(project_root: Path, *, scale: int) -> tuple[GeneratedTable, ...]:
    """Rewrite the project's CSV fixtures in place at `scale` replicas; return what was written."""
    if scale < 1:
        raise BenchError(f"scale must be at least 1, got {scale}")
    lakehouse_root = project_root / "lakehouse_code"
    keys = primary_key_columns(lakehouse_root)
    key_columns = frozenset(keys.values())
    tables = []
    for source in inventory_for(project_root).sources:
        fixture_dir = lakehouse_root / "bronze" / source.name / FIXTURE_DIR
        for resource in source.resources:
            path = fixture_dir / f"{resource.name}.csv"
            if not path.is_file():
                raise BenchError(f"{path}: no CSV fixture for {source.name}/{resource.name}")
            tables.append(_scale_file(path, source.name, resource.name, key_columns, scale))
    return tuple(tables)


def copy_project(project_root: Path, destination: Path) -> Path:
    """Copy the profile and `lakehouse_code/` -- what a project build reads -- to a scratch root."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(project_root / "openlakeforge.yaml", destination / "openlakeforge.yaml")
    shutil.copytree(
        project_root / "lakehouse_code",
        destination / "lakehouse_code",
        ignore=shutil.ignore_patterns("__pycache__", "target", "dbt_packages", "logs"),
    )
    return destination


def _scale_file(path: Path, source: str, resource: str, key_columns: frozenset[str], scale: int) -> GeneratedTable:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    rewritten = [column for column in fieldnames if column in key_columns]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for replica in range(scale):
            for row in rows:
                if replica:
                    row = {**row, **{column: f"{row[column]}-R{replica}" for column in rewritten}}
                writer.writerow(row)
    payload = path.read_bytes()
    return GeneratedTable(
        source=source,
        resource=resource,
        rows=len(rows) * scale,
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
