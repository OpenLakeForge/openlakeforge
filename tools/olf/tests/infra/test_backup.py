"""The order of `olf infra backup` and what it always undoes (#910). The cluster is a fake `Operations`."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from _backup_support import full_contracts

from olf.infra.backup import BackupError
from olf.infra.backup._backup import Operations, run_backup
from olf.infra.backup._bundle import decrypt_secrets
from olf.infra.backup._manifest import load_manifest, verify_checksums
from olf.infra.backup._plan import BackupPlan, plan_from_contract
from olf.infra.e2e._secret_exposure import find_exposures

PASSPHRASE = "correct horse battery"
STAGES = {"dev": "olf-dev", "prod": "olf-prod"}
ADMIN_PASSWORD = "A-very-secret-admin-password-0123"


class Cluster:
    """Records every call, in order, and serves what the stack would."""

    def __init__(self, *, runs: dict[str, list[str]] | None = None, fail_on: str = "") -> None:
        self.events: list[str] = []
        self.replicas = {"olf-dev": 1, "olf-prod": 1}
        self.runs = runs or {}
        self.fail_on = fail_on
        self.secrets: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}

    def serve(self, plan: BackupPlan) -> None:
        encoded = base64.b64encode(ADMIN_PASSWORD.encode()).decode()
        self.secrets = {(ref.namespace, ref.name): ("Opaque", {"password": encoded}) for ref in plan.secrets}

    def scale(self, namespace: str, replicas: int) -> None:
        self.events.append(f"scale {namespace} {replicas}")
        self.replicas[namespace] = replicas

    def dump(self, name: str, destination: Path) -> None:
        self.events.append(f"dump {name}")
        if name == self.fail_on:
            raise RuntimeError(f"pg_dump of {name} failed")
        destination.write_bytes(b"PGDMP" + name.encode())

    def copy(self, bucket: str, destination: Path) -> dict[str, int]:
        self.events.append(f"copy {bucket}")
        (destination / "silver").mkdir(parents=True)
        (destination / "silver" / "a.parquet").write_bytes(b"12345")
        return {"silver/a.parquet": 5}

    def operations(self) -> Operations:
        return Operations(
            daemon_replicas=lambda namespace: self.replicas[namespace],
            scale_daemon=self.scale,
            runs_in_flight=lambda namespace: self.runs.get(namespace, []),
            dump_database=self.dump,
            copy_bucket=self.copy,
            read_secret=lambda namespace, name: self.secrets.get((namespace, name)),
        )


def _backup(directory: Path, cluster: Cluster) -> object:
    plan = plan_from_contract(full_contracts(), shared_namespace="olf-system", github_oauth_secret="github-oauth-app")
    cluster.serve(plan)
    return run_backup(
        directory,
        plan=plan,
        stages=STAGES,
        release="0.6.0",
        profile_sha256="ab" * 32,
        provider="local",
        passphrase=PASSPHRASE,
        ops=cluster.operations(),
    )


def test_polaris_is_dumped_before_every_other_database_and_before_the_first_object_copy(tmp_path: Path) -> None:
    cluster = Cluster()

    _backup(tmp_path / "backup", cluster)

    dumps = [event for event in cluster.events if event.startswith("dump ")]
    assert dumps[0] == "dump polaris" and len(dumps) == 7
    first_copy = next(i for i, event in enumerate(cluster.events) if event.startswith("copy "))
    assert cluster.events.index("dump polaris") < first_copy
    assert max(cluster.events.index(event) for event in dumps) < first_copy


def test_the_daemon_is_paused_first_and_resumed_to_what_it_ran_with(tmp_path: Path) -> None:
    cluster = Cluster()
    cluster.replicas["olf-prod"] = 2

    _backup(tmp_path / "backup", cluster)

    assert cluster.events[:4] == ["scale olf-dev 0", "scale olf-prod 0", "dump polaris", "dump dagster_dev"]
    assert cluster.events[-2:] == ["scale olf-dev 1", "scale olf-prod 2"]
    assert load_manifest(tmp_path / "backup").dagster_paused is True


def test_the_daemon_is_resumed_when_a_step_raises(tmp_path: Path) -> None:
    cluster = Cluster(fail_on="dagster_prod")

    with pytest.raises(RuntimeError, match="pg_dump of dagster_prod failed"):
        _backup(tmp_path / "backup", cluster)

    assert cluster.events[-2:] == ["scale olf-dev 1", "scale olf-prod 1"]
    assert not (tmp_path / "backup" / "manifest.json").exists()


def test_a_run_in_flight_refuses_the_backup_and_the_daemon_still_resumes(tmp_path: Path) -> None:
    cluster = Cluster(runs={"olf-prod": ["run-7"]})

    with pytest.raises(BackupError, match=r"in flight \(prod: run-7\)"):
        _backup(tmp_path / "backup", cluster)

    assert not any(event.startswith(("dump ", "copy ")) for event in cluster.events)
    assert cluster.replicas == {"olf-dev": 1, "olf-prod": 1}


def test_a_daemon_that_does_not_come_back_is_reported_with_the_command_that_resumes_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = Cluster()
    scale = cluster.scale

    def refuses_to_resume(namespace: str, replicas: int) -> None:
        if replicas:
            raise RuntimeError("the API server is gone")
        scale(namespace, replicas)

    operations = replace(cluster.operations(), scale_daemon=refuses_to_resume)
    plan = plan_from_contract(full_contracts(), shared_namespace="olf-system")
    cluster.serve(plan)

    with pytest.raises(BackupError, match="kubectl scale deployment/dagster-daemon -n olf-dev --replicas=1"):
        run_backup(
            tmp_path / "backup",
            plan=plan,
            stages=STAGES,
            release="0.6.0",
            profile_sha256="",
            provider="local",
            passphrase=PASSPHRASE,
            ops=operations,
        )

    assert "could not resume the Dagster daemon in olf-prod" in capsys.readouterr().err


def test_the_directory_holds_the_layout_a_restore_reads(tmp_path: Path) -> None:
    directory = tmp_path / "backup"

    _backup(directory, Cluster())

    manifest = load_manifest(directory)
    assert verify_checksums(directory) == []
    assert (manifest.release, manifest.provider, manifest.stages) == ("0.6.0", "local", ("dev", "prod"))
    assert [d.name for d in manifest.databases][0] == "polaris"
    assert (directory / "postgres" / "polaris.dump").read_bytes() == b"PGDMPpolaris"
    assert (directory / "objects" / "lakehouse-dev-gold" / "silver" / "a.parquet").read_bytes() == b"12345"
    assert json.loads((directory / "objects" / "lakehouse-dev-gold.inventory.json").read_text()) == {
        "silver/a.parquet": 5
    }
    assert [(b.name, b.objects, b.bytes) for b in manifest.buckets][-1] == ("openlakeforge-ops", 1, 5)
    bundle = decrypt_secrets((directory / "secrets.enc").read_bytes(), PASSPHRASE)
    assert {(s["namespace"], s["name"]) for s in bundle} == {(s.namespace, s.name) for s in manifest.secrets}
    assert all(s.keys == ("password",) for s in manifest.secrets)


def test_no_secret_value_is_in_the_directory_or_the_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "backup"

    _backup(directory, Cluster())

    output = capsys.readouterr()
    surfaces = {
        str(path.relative_to(directory)): path.read_bytes().decode("latin-1")
        for path in directory.rglob("*")
        if path.is_file()
    }
    surfaces["the log"] = output.out + output.err
    assert find_exposures({"admin password": ADMIN_PASSWORD}, surfaces) == []
    assert PASSPHRASE not in "".join(surfaces.values())


def test_a_secret_the_cluster_lacks_fails_the_backup_naming_it(tmp_path: Path) -> None:
    cluster = Cluster()
    plan = plan_from_contract(full_contracts(), shared_namespace="olf-system")
    cluster.serve(plan)
    del cluster.secrets[("olf-system", "openmetadata-fernet-key")]

    with pytest.raises(BackupError, match="olf-system/openmetadata-fernet-key"):
        run_backup(
            tmp_path / "backup",
            plan=plan,
            stages=STAGES,
            release="0.6.0",
            profile_sha256="",
            provider="local",
            passphrase=PASSPHRASE,
            ops=cluster.operations(),
        )

    assert cluster.replicas == {"olf-dev": 1, "olf-prod": 1}


def test_a_backup_does_not_go_into_a_directory_that_holds_something(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("x")

    with pytest.raises(BackupError, match="not empty"):
        _backup(tmp_path, Cluster())
