"""Restore a backup (#911) onto a platform of the same release and profile that is already deployed.

The order is the point. Nothing is changed until `_preflight.preflight` has passed. The Dagster daemons are scaled
to 0 first and the runs already launched (separate Kubernetes Jobs that scaling a Deployment does not stop) are
counted: any run in flight refuses the restore before anything else is touched. Then the workloads that hold the
databases open are scaled to 0. The Secrets come back next, because the restored `polaris` database names
principals whose credentials only the restored Secrets hold. Then each database is recreated and loaded, and each
bucket is made equal to the backup's. Scaling back up is the last step; the reconcile `olf infra deploy` is the
caller's, so that the Polaris bootstrap keeps the restored principals instead of minting new ones.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from olf.common import config, log
from olf.infra import k8s, s3
from olf.infra.backup import BackupError
from olf.infra.backup._backup import DAEMON_DEPLOYMENT, refuse_runs_in_flight
from olf.infra.backup._manifest import DatabaseDump, Manifest, SecretRef
from olf.infra.backup._preflight import is_workload, preflight
from olf.infra.e2e._dagster import DAGSTER_WEBSERVER_SERVICE_NAME

POSTGRES_POD = "postgresql-0"
POSTGRES_CONTAINER = "postgresql"
# Deployments in the shared namespace that hold a database open or read the restored Secrets. SeaweedFS, Traefik
# and cert-manager stay up: the objects are uploaded through SeaweedFS, and cert-manager owns `local-ca`.
SHARED_WORKLOADS = ("openmetadata", "polaris", "trino", "dex", "platform-api")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def database_statements(name: str, owner: str) -> tuple[list[str], list[str]]:
    """The SQL that recreates a database as `postgresql/templates/init.sh.tftpl` makes it.

    Returns the statements for the `postgres` database and those for the new one. PostgreSQL grants CONNECT on
    a new database to PUBLIC, so every other stage's role could otherwise sign in to it (#225).
    """
    for identifier in (name, owner):
        if not _IDENTIFIER.fullmatch(identifier):
            raise BackupError(f"refusing database or role name {identifier!r}: only letters, digits and _ are allowed")
    database, role = f'"{name}"', f'"{owner}"'
    return (
        [
            f"DROP DATABASE IF EXISTS {database} WITH (FORCE)",
            f"CREATE DATABASE {database} OWNER {role}",
            f"GRANT ALL PRIVILEGES ON DATABASE {database} TO {role}",
            f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC",
            f"GRANT CONNECT ON DATABASE {database} TO {role}",
        ],
        [f"GRANT ALL ON SCHEMA public TO {role}", f"ALTER SCHEMA public OWNER TO {role}"],
    )


def _inside(directory: Path, relative: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise BackupError(f"{relative!r} leaves the backup directory")
    return path


def _clients(
    shared_namespace: str, stage_namespaces: tuple[str, ...], environ: Mapping[str, str] | None
) -> dict[tuple[str, str], int]:
    """The Deployments to stop, each with the replica count it runs (or was left with by a failed restore)."""
    workloads: dict[tuple[str, str], int] = {}
    for namespace in dict.fromkeys(stage_namespaces):
        if namespace != shared_namespace:
            workloads |= {
                (namespace, name): count
                for name, count in k8s.deployment_replica_counts(namespace, environ=environ).items()
            }
    shared = k8s.deployment_replica_counts(shared_namespace, environ=environ)
    workloads |= {
        (shared_namespace, name): count for name, count in shared.items() if is_workload(name, SHARED_WORKLOADS)
    }
    return {key: count for key, count in workloads.items() if count > 0}


def _scale_down(workloads: Mapping[tuple[str, str], int], environ: Mapping[str, str] | None) -> None:
    """Scale each Deployment to 0, keeping its count on it so that a rerun scales it back to the same."""
    for (namespace, name), count in workloads.items():
        k8s.scale_deployment(name, namespace, 0, remember=count, environ=environ)
        log.info(f"Scaled {namespace}/{name} to 0 (was {count}).")


def _scale_up(
    workloads: Mapping[tuple[str, str], int], shared_namespace: str, environ: Mapping[str, str] | None
) -> None:
    """Scale back up in the order the platform depends on: the shared services, the stages' Deployments, and last
    the daemons, which launch the scheduled and catch-up runs that use all of them."""

    def rank(key: tuple[str, str]) -> int:
        return 0 if key[0] == shared_namespace else 2 if key[1] == DAEMON_DEPLOYMENT else 1

    for (namespace, name), count in sorted(workloads.items(), key=lambda item: rank(item[0])):
        k8s.scale_deployment(name, namespace, count, remember=0, environ=environ)
        log.info(f"Scaled {namespace}/{name} back to {count}.")


def _refuse_active_runs(
    stage_namespaces: Mapping[str, str],
    runs_in_flight: Callable[[str], list[str]],
    daemons: Mapping[tuple[str, str], int],
    shared_namespace: str,
    environ: Mapping[str, str] | None,
) -> None:
    """Stop the daemons so that no run starts, then refuse while one is in flight, resuming them if so.

    A stage whose webserver is already at 0 was stopped by an earlier failed restore, which passed this check with
    its daemon stopped before it: it is not asked again, since there is nothing to ask.
    """

    def serving_runs(namespace: str) -> list[str]:
        if not k8s.deployment_replicas(DAGSTER_WEBSERVER_SERVICE_NAME, namespace, environ=environ):
            return []
        return runs_in_flight(namespace)

    try:
        _scale_down(daemons, environ)
        refuse_runs_in_flight(stage_namespaces, serving_runs, action="restore")
    except BaseException:
        _scale_up(daemons, shared_namespace, environ)
        raise


def _replace_secrets(
    refs: tuple[SecretRef, ...], secrets: list[dict[str, Any]], environ: Mapping[str, str] | None
) -> None:
    """Delete and recreate each Secret, so it holds exactly the backed-up keys under the backed-up type.

    Names come from the manifest, never from the decrypted bundle, and a failure reports the name only, so nothing
    read out of the bundle reaches a log line or an error.
    """
    bundled = {(secret["namespace"], secret["name"]): secret for secret in secrets}
    for ref in refs:
        secret = bundled[(ref.namespace, ref.name)]
        try:
            data = {key: base64.b64decode(value).decode("utf-8") for key, value in secret["data"].items()}
        except (binascii.Error, UnicodeDecodeError):
            raise BackupError(f"Secret {ref.namespace}/{ref.name} does not hold text values") from None
        k8s.delete_secret(ref.name, ref.namespace, environ=environ)
        try:
            k8s.write_secret(
                ref.name,
                ref.namespace,
                data,
                overwrite=False,
                secret_type=secret.get("type") or "Opaque",
                environ=environ,
            )
        except k8s.KubectlError:
            raise BackupError(f"could not write Secret {ref.namespace}/{ref.name}") from None
        log.info(f"Restored Secret {ref.namespace}/{ref.name}.")


def _restore_database(directory: Path, dump: DatabaseDump, namespace: str, environ: Mapping[str, str] | None) -> None:
    server, inside = database_statements(dump.name, dump.owner)

    def psql(database: str, statements: list[str]) -> None:
        argv = ["psql", "-v", "ON_ERROR_STOP=1", "--username", "postgres", "--dbname", database]
        for statement in statements:
            argv += ["-c", statement]
        k8s.exec_in_pod(POSTGRES_POD, namespace, argv, container=POSTGRES_CONTAINER, environ=environ)

    psql("postgres", server)
    psql(dump.name, inside)
    k8s.exec_in_pod(
        POSTGRES_POD,
        namespace,
        [
            "pg_restore",
            "--username",
            "postgres",
            "--dbname",
            dump.name,
            "--no-owner",
            "--no-acl",
            f"--role={dump.owner}",
        ],
        container=POSTGRES_CONTAINER,
        stdin_path=_inside(directory, dump.file),
        environ=environ,
    )
    log.info(f"Restored database {dump.name}.")


def _listing(client: Any, bucket: str) -> dict[str, int]:
    return {
        item["Key"]: item["Size"]
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        for item in page.get("Contents", [])
    }


def _upload_objects(
    directory: Path, manifest: Manifest, shared_namespace: str, environ: Mapping[str, str] | None
) -> None:
    """Make each bucket hold exactly the backup's objects: upload them, delete the rest, then list and compare.

    Objects written after the backup are deleted, because the pipelines read whatever a prefix holds (Silver globs
    every `*.csv` in Bronze), so a stray file would be ingested as data the backup never held.
    """
    if not manifest.buckets:
        return
    identity = {
        key: k8s.secret_value("seaweedfs-s3-creds", key, shared_namespace, environ=environ)
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    }
    with s3.port_forward_client(
        manifest.buckets[0].name,
        service="seaweedfs-s3",
        remote_port=8333,
        namespace=shared_namespace,
        access_key_id=identity["AWS_ACCESS_KEY_ID"],
        secret_access_key=identity["AWS_SECRET_ACCESS_KEY"],
        region=config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1", environ=environ),
        environ=environ,
    ) as client:
        for bucket in manifest.buckets:
            inventory: dict[str, int] = json.loads(_inside(directory, bucket.inventory_file).read_text())
            for key in sorted(inventory):
                client.upload_file(str(_inside(directory, f"objects/{bucket.name}/{key}")), bucket.name, key)
            # ponytail: one request per key; a bucket with thousands of post-backup objects would want delete_objects.
            for key in sorted(set(_listing(client, bucket.name)) - set(inventory)):
                client.delete_object(Bucket=bucket.name, Key=key)
            present = _listing(client, bucket.name)
            if present != inventory:
                wrong = sorted(
                    key for key in inventory.keys() | present.keys() if present.get(key) != inventory.get(key)
                )
                raise BackupError(f"bucket {bucket.name} does not match its inventory (key and size) at: {wrong[:5]}")
            log.info(f"Restored bucket {bucket.name}: {len(inventory)} objects.")


def restore(
    directory: Path,
    *,
    passphrase: str,
    release: str,
    profile: Path,
    stage_namespaces: Mapping[str, str],
    shared_namespace: str,
    runs_in_flight: Callable[[str], list[str]],
    environ: Mapping[str, str] | None,
) -> None:
    """Replace the platform's state with the backup's, leaving its workloads running again.

    Nothing is changed until the backup and the platform are checked and no Dagster run is in flight. A failure
    after that leaves the workloads scaled to 0, so nothing serves half-restored data; the error says how to recover.
    """
    manifest, secrets = preflight(
        directory,
        passphrase=passphrase,
        release=release,
        profile=profile,
        stage_namespaces=stage_namespaces,
        shared_namespace=shared_namespace,
        environ=environ,
    )
    workloads = _clients(shared_namespace, tuple(stage_namespaces.values()), environ)
    daemons = {key: count for key, count in workloads.items() if key[1] == DAEMON_DEPLOYMENT}
    _refuse_active_runs(stage_namespaces, runs_in_flight, daemons, shared_namespace, environ)
    namespaces = tuple(dict.fromkeys(stage_namespaces.values()))
    try:
        for namespace in namespaces:  # the log archiver uploads to the ops bucket every 15 minutes, even when idle
            k8s.suspend_cronjobs(namespace, environ=environ)
        _scale_down({key: count for key, count in workloads.items() if key not in daemons}, environ)
        _replace_secrets(manifest.secrets, secrets, environ)
        for dump in manifest.databases:
            _restore_database(directory, dump, shared_namespace, environ)
        _upload_objects(directory, manifest, shared_namespace, environ)
        _scale_up(workloads, shared_namespace, environ)
        for namespace in namespaces:
            k8s.resume_cronjobs(namespace, environ=environ)
    except Exception as exc:
        left = ", ".join(f"{namespace}/{name} (was {count})" for (namespace, name), count in workloads.items())
        raise BackupError(
            f"restore failed: {exc}\nThe workloads were left scaled to 0 so nothing serves half-restored data, "
            f"and the CronJobs stay suspended: {left}.\nFix the cause and run the same "
            f"`olf infra restore --from {directory}` again; it replaces everything and remembers each workload's "
            "replica count and each CronJob's suspension."
        ) from exc
