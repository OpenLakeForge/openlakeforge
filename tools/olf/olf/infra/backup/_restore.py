"""The steps that put a backup's data back (#911): Secrets, databases and objects.

Each step replaces what is there with what the backup holds, and none of them stops the workloads that use it;
the caller does that first. Secret names come from the manifest, never from the decrypted bundle, so nothing read
out of `secrets.enc` reaches a log line or an error.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from olf.common import config, log
from olf.infra import k8s, s3
from olf.infra.backup import BackupError
from olf.infra.backup._manifest import DatabaseDump, Manifest, SecretRef

POSTGRES_POD = "postgresql-0"
POSTGRES_CONTAINER = "postgresql"
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
