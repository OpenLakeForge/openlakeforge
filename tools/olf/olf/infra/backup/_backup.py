"""`olf infra backup`: the order of a backup and what is always undone.

The cluster is reached through `Operations`, one callable per interaction, so the order and the cleanup are tested
without one (as `e2e._durability.Sources` does for the durability check). `_cluster.cluster_operations` binds them.

The Dagster daemon is stopped for the length of the backup: it launches the scheduled and queued runs that would
write Iceberg data and Polaris pointers between one copy and the next. Runs already started are not stopped, so a
backup refuses to begin while any is in flight. The daemon is resumed in a `finally`, whatever failed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from olf.common import log
from olf.infra.backup import BackupError
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
from olf.infra.backup._plan import POLARIS_DATABASE, BackupPlan

DAEMON_DEPLOYMENT = "dagster-daemon"
_PG_DUMP_MAGIC = b"PGDMP"


@dataclass(frozen=True)
class Operations:
    daemon_replicas: Callable[[str], int]  # namespace -> replicas the daemon runs with
    scale_daemon: Callable[[str, int], None]  # namespace, replicas; returns once the daemon has stopped or is ready
    runs_in_flight: Callable[[str], list[str]]  # namespace -> run ids that are queued or running
    dump_database: Callable[[str, Path], None]  # database, file to write a pg_dump -Fc archive to
    copy_bucket: Callable[[str, Path], dict[str, int]]  # bucket, directory to copy into -> {key: size}
    read_secret: Callable[[str, str], tuple[str, dict[str, str]] | None]  # namespace, name -> (type, base64 data)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_backup(
    directory: Path,
    *,
    plan: BackupPlan,
    stages: Mapping[str, str],
    release: str,
    profile_sha256: str,
    provider: str,
    passphrase: str,
    ops: Operations,
    clock: Callable[[], str] = _now,
) -> Manifest:
    """Write the backup of `plan` into `directory` and return its manifest. `stages` maps stage name to namespace.

    A failed backup leaves what it had written, and no manifest.json, so `restore` has nothing to trust in it.
    """
    if directory.exists() and any(directory.iterdir()):
        raise BackupError(f"{directory} is not empty; a backup goes into a new directory.")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    started_at = clock()
    paused: dict[str, int] = {}
    try:
        for namespace in dict.fromkeys(stages.values()):
            log.step(f"Pausing the Dagster daemon in {namespace}...")
            paused[namespace] = ops.daemon_replicas(namespace)
            ops.scale_daemon(namespace, 0)
        refuse_runs_in_flight(stages, ops.runs_in_flight, action="back up")
        databases = _dump_databases(directory, plan, ops)
        buckets = _copy_buckets(directory, plan, ops)
        secrets = _bundle_secrets(directory, plan, passphrase, ops)
        manifest = Manifest(
            format=FORMAT,
            release=release,
            profile_sha256=profile_sha256,
            provider=provider,
            stages=tuple(sorted(stages)),
            databases=databases,
            buckets=buckets,
            secrets=secrets,
            dagster_paused=True,
            started_at=started_at,
            finished_at=clock(),
        )
        write_manifest(directory, manifest)
    finally:
        failures = _resume_daemons(paused, ops)
    if failures:
        raise BackupError("the backup is written, but the Dagster daemon did not come back:\n" + "\n".join(failures))
    return manifest


def refuse_runs_in_flight(
    stages: Mapping[str, str], runs_in_flight: Callable[[str], list[str]], *, action: str
) -> None:
    """Raise when any stage (stage name -> namespace) has a Dagster run queued or running."""
    busy = {stage: runs for stage, namespace in stages.items() if (runs := runs_in_flight(namespace))}
    if busy:
        found = "; ".join(f"{stage}: {', '.join(runs)}" for stage, runs in sorted(busy.items()))
        raise BackupError(
            f"refusing to {action} while Dagster runs are in flight ({found}). Wait for them, then retry."
        )


def _dump_databases(directory: Path, plan: BackupPlan, ops: Operations) -> tuple[DatabaseDump, ...]:
    (directory / "postgres").mkdir()
    # Polaris first: an Iceberg commit writes its data files before the pointer, so every pointer in this dump
    # names files the later object copy holds.
    ordered = sorted(plan.databases, key=lambda database: database[0] != POLARIS_DATABASE[0])
    dumps = []
    for name, owner in ordered:
        log.step(f"Dumping database {name}...")
        relative = f"postgres/{name}.dump"
        ops.dump_database(name, directory / relative)
        path = directory / relative
        with path.open("rb") as handle:
            if handle.read(len(_PG_DUMP_MAGIC)) != _PG_DUMP_MAGIC:
                raise BackupError(f"the dump of database {name} is not a pg_dump archive.")
        dumps.append(DatabaseDump(name, owner, relative, sha256_file(path), path.stat().st_size))
    return tuple(dumps)


def _copy_buckets(directory: Path, plan: BackupPlan, ops: Operations) -> tuple[BucketCopy, ...]:
    copies = []
    for bucket in plan.buckets:
        log.step(f"Copying bucket {bucket}...")
        inventory = ops.copy_bucket(bucket, directory / "objects" / bucket)
        relative = f"objects/{bucket}.inventory.json"
        (directory / relative).write_text(json.dumps(inventory, indent=1, sort_keys=True) + "\n")
        copies.append(BucketCopy(bucket, len(inventory), sum(inventory.values()), relative))
    return tuple(copies)


def _bundle_secrets(directory: Path, plan: BackupPlan, passphrase: str, ops: Operations) -> tuple[SecretRef, ...]:
    log.step("Sealing the Secrets...")
    sealed, refs, missing = [], [], []
    for ref in plan.secrets:
        secret = ops.read_secret(ref.namespace, ref.name)
        if secret is None:
            missing.append(f"{ref.namespace}/{ref.name}")
            continue
        kind, data = secret
        sealed.append({"namespace": ref.namespace, "name": ref.name, "type": kind, "data": data})
        refs.append(SecretRef(ref.namespace, ref.name, tuple(sorted(data))))
    if missing:
        raise BackupError("the Secrets a restore needs are not in the cluster: " + ", ".join(missing))
    (directory / "secrets.enc").write_bytes(encrypt_secrets(sealed, passphrase))
    return tuple(refs)


def _resume_daemons(paused: Mapping[str, int], ops: Operations) -> list[str]:
    failures = []
    for namespace, replicas in paused.items():
        try:
            ops.scale_daemon(namespace, replicas)
        except Exception as exc:  # every namespace is tried, and the failure is reported, not raised over the cause
            failures.append(
                f"{namespace}: {exc}. Resume it with: kubectl scale deployment/{DAEMON_DEPLOYMENT} "
                f"-n {namespace} --replicas={replicas}"
            )
    for failure in failures:
        log.error(f"could not resume the Dagster daemon in {failure}")
    return failures
