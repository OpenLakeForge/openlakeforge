"""Stateful fakes for the data a restore replaces: Secrets, PostgreSQL databases and object storage (#911)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from olf.infra import k8s, s3

SHARED = "olf-system"


class FakeS3:
    """Buckets of {key: size}; an upload takes the file's size."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, int]] = {}
        self.shrink = False  # an upload that arrives one byte short
        self.undeletable: set[str] = set()  # keys a delete leaves in place

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        size = Path(filename).stat().st_size
        self.objects.setdefault(bucket, {})[key] = size - 1 if self.shrink else size

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        if Key not in self.undeletable:
            self.objects[Bucket].pop(Key, None)

    def get_paginator(self, _operation: str) -> Any:
        outer = self

        class Paginator:
            def paginate(self, *, Bucket: str) -> list[dict[str, Any]]:
                return [{"Contents": [{"Key": k, "Size": v} for k, v in outer.objects.get(Bucket, {}).items()]}]

        return Paginator()


@dataclass
class Database:
    owner: str
    connect: set[str] = field(default_factory=lambda: {"PUBLIC"})  # PostgreSQL grants CONNECT to PUBLIC on creation
    privileges: set[str] = field(default_factory=set)
    schema_owner: str = "postgres"
    data: bytes = b""


class FakePostgres:
    """The databases, their owners and grants, as the SQL and `pg_restore` change them."""

    def __init__(self) -> None:
        self.databases: dict[str, Database] = {}
        self.failing = False

    def psql(self, argv: list[str]) -> None:
        database = argv[argv.index("--dbname") + 1]
        for index, part in enumerate(argv):
            if part == "-c":
                self._run(database, argv[index + 1])

    def _run(self, database: str, sql: str) -> None:
        def match(pattern: str) -> re.Match[str]:
            found = re.fullmatch(pattern, sql)
            assert found, f"the fake PostgreSQL does not know: {sql}"
            return found

        if sql.startswith("DROP DATABASE"):
            self.databases.pop(match(r'DROP DATABASE IF EXISTS "(\w+)" WITH \(FORCE\)')[1], None)
        elif sql.startswith("CREATE DATABASE"):
            name, owner = match(r'CREATE DATABASE "(\w+)" OWNER "(\w+)"').groups()
            assert name not in self.databases, f"database {name} already exists"
            self.databases[name] = Database(owner)
        elif sql.startswith("GRANT ALL PRIVILEGES ON DATABASE"):
            name, role = match(r'GRANT ALL PRIVILEGES ON DATABASE "(\w+)" TO "(\w+)"').groups()
            self.databases[name].privileges.add(role)
        elif sql.startswith("REVOKE CONNECT"):
            self.databases[match(r'REVOKE CONNECT ON DATABASE "(\w+)" FROM PUBLIC')[1]].connect.discard("PUBLIC")
        elif sql.startswith("GRANT CONNECT"):
            name, role = match(r'GRANT CONNECT ON DATABASE "(\w+)" TO "(\w+)"').groups()
            self.databases[name].connect.add(role)
        elif sql.startswith("ALTER SCHEMA public OWNER"):
            self.databases[database].schema_owner = match(r'ALTER SCHEMA public OWNER TO "(\w+)"')[1]
        else:
            match(r'GRANT ALL ON SCHEMA public TO "\w+"')

    def pg_restore(self, argv: list[str], dump: Path) -> None:
        if self.failing:
            raise k8s.KubectlError("pg_restore failed: boom")
        database = self.databases[argv[argv.index("--dbname") + 1]]
        role = next(part.split("=", 1)[1] for part in argv if part.startswith("--role="))
        if database.schema_owner != role:  # the dump creates its tables in `public`
            raise k8s.KubectlError(f"permission denied for schema public: {role} does not own it")
        database.data = dump.read_bytes()


class DataPlane:
    """Secrets, PostgreSQL and object storage, patched over `olf.infra.k8s` and `olf.infra.s3`.

    `require_quiet` is called before each change; a subclass raises there if the cluster is not ready for one.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.secrets: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
        self.secret_error: str | None = None
        self.postgres = FakePostgres()
        self.s3 = FakeS3()
        monkeypatch.setattr(k8s, "delete_secret", self.delete_secret)
        monkeypatch.setattr(k8s, "write_secret", self.write_secret)
        monkeypatch.setattr(k8s, "exec_in_pod", self.exec_in_pod)
        monkeypatch.setattr(k8s, "secret_value", lambda *_a, **_k: "admin")
        monkeypatch.setattr(s3, "port_forward_client", self.port_forward_client)

    def require_quiet(self, what: str) -> None:
        """Hook: raise when `what` may not change now."""

    def delete_secret(self, name: str, namespace: str, **_: Any) -> None:
        self.require_quiet("a Secret was replaced")
        self.secrets.pop((namespace, name), None)

    def write_secret(self, name: str, namespace: str, data: dict[str, str], **kwargs: Any) -> None:
        self.require_quiet("a Secret was written")
        if self.secret_error:
            raise k8s.KubectlError(f"{self.secret_error} {data}")
        assert (namespace, name) not in self.secrets and not kwargs["overwrite"]
        self.secrets[(namespace, name)] = (kwargs["secret_type"], data)

    def exec_in_pod(self, pod: str, namespace: str, argv: list[str], **kwargs: Any) -> None:
        assert (pod, namespace, kwargs["container"]) == ("postgresql-0", SHARED, "postgresql")
        self.require_quiet("PostgreSQL changed")
        if argv[0] == "psql":
            self.postgres.psql(argv)
        else:
            assert argv[0] == "pg_restore"
            self.postgres.pg_restore(argv, kwargs["stdin_path"])

    @contextmanager
    def port_forward_client(self, bucket: str, **_: Any) -> Iterator[FakeS3]:
        self.require_quiet("object storage changed")
        yield self.s3
