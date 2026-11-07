"""`olf infra restore` (#911): what the whole restore leaves behind, against a fake cluster that keeps its state.

The fake refuses the states that would corrupt a restore (data replaced while a client runs or a CronJob can
start, a run launched while the daemon runs, a daemon resumed before the services it needs), so the tests check
where the cluster ends up, not which calls were made.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from _backup_fixture import PASSPHRASE, SECRETS, SHARED, STAGES, Backup
from _ca_fakes import PUBLISH_JOB, FakeCaTrust
from _restore_fakes import Database, DataPlane
from typer.testing import CliRunner

from olf.cli import app
from olf.infra import k8s
from olf.infra.backup import BackupError
from olf.infra.backup._backup import DAEMON_DEPLOYMENT
from olf.infra.backup._manifest import SecretRef
from olf.infra.backup._restore import CA_CONFIGMAP, SHARED_WORKLOADS, reissue_after_reconcile, restore
from olf.infra.e2e._dagster import DAGSTER_WEBSERVER_SERVICE_NAME

_ENVIRON = {"KUBE_CONTEXT": "kind-test"}
_SILVER = "lakehouse-dev-silver"
_ARCHIVE = ("olf-dev", "openlakeforge-k8s-log-archive")


class FakeCluster(DataPlane):
    """Deployments and CronJobs on top of the data plane, with the rules a restore must not break."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch)
        self.replicas = {
            ("olf-dev", DAGSTER_WEBSERVER_SERVICE_NAME): 1,
            ("olf-dev", DAEMON_DEPLOYMENT): 1,
            ("olf-dev", "superset"): 2,
            ("olf-prod", DAGSTER_WEBSERVER_SERVICE_NAME): 1,
            ("olf-prod", DAEMON_DEPLOYMENT): 1,
            (SHARED, "polaris"): 1,
            (SHARED, "trino-coordinator"): 1,
            (SHARED, "dex"): 1,
            (SHARED, "seaweedfs-s3"): 1,
            (SHARED, "traefik"): 1,
        }
        self.started = dict(self.replicas)
        self.remembered: dict[tuple[str, str], int] = {}
        self.stopped: set[tuple[str, str]] = set()  # every Deployment that was ever scaled to 0
        # CronJob -> [suspended now, suspended before the restore (None: not remembered)]
        self.cronjobs: dict[tuple[str, str], list[bool | None]] = {_ARCHIVE: [False, None]}
        self.deployed = True
        self.runs: dict[str, list[str]] = {}  # namespace -> Dagster runs in flight
        self.ca = FakeCaTrust(monkeypatch, self.secrets, self.replicas)
        self.ca.add_certificate(SHARED, "local-ca-probe", "local-ca-probe")
        self.ca.add_certificate(SHARED, "trino-tls", "trino-tls")
        self.ca.add_certificate(SHARED, "ingress-shim", "superset-dev-tls")
        self.ca.add_certificate(SHARED, "not-ours", "other-tls", issuer="letsencrypt")
        self.ca.mounts = {
            (SHARED, "polaris"): {("configmap", CA_CONFIGMAP)},
            (SHARED, "dex"): {("configmap", CA_CONFIGMAP)},
            (SHARED, "trino-coordinator"): {("secret", "trino-tls")},
            (SHARED, "traefik"): {("secret", "other-tls")},
        }
        self.ca.deploy_with("OLD-CA")
        monkeypatch.setattr(k8s, "resource_exists", lambda *_a, **_k: self.deployed)
        monkeypatch.setattr(k8s, "deployment_replica_counts", self.deployment_replica_counts)
        monkeypatch.setattr(k8s, "deployment_replicas", lambda name, ns, **_k: self.replicas[(ns, name)])
        monkeypatch.setattr(k8s, "scale_deployment", self.scale_deployment)
        monkeypatch.setattr(k8s, "suspend_cronjobs", self.suspend_cronjobs)
        monkeypatch.setattr(k8s, "resume_cronjobs", self.resume_cronjobs)

    def is_client(self, namespace: str, name: str) -> bool:
        """A Deployment that holds a database open or reads a restored Secret."""
        return namespace != SHARED or any(name == p or name.startswith(f"{p}-") for p in SHARED_WORKLOADS)

    def running_clients(self) -> list[tuple[str, str]]:
        return [key for key, count in self.replicas.items() if count and self.is_client(*key)]

    def require_quiet(self, what: str) -> None:
        assert not self.running_clients(), f"{what} while {self.running_clients()} still run"
        assert all(suspended for suspended, _ in self.cronjobs.values()), f"{what} while a CronJob can start"

    def deployment_replica_counts(self, namespace: str, **_: Any) -> dict[str, int]:
        return {
            name: (self.remembered.get((ns, name), 0) if count == 0 else count)
            for (ns, name), count in self.replicas.items()
            if ns == namespace
        }

    def scale_deployment(
        self, name: str, namespace: str, replicas: int, *, remember: int | None = None, **_: Any
    ) -> None:
        if replicas and name == DAEMON_DEPLOYMENT:  # it launches runs, so everything they use is up first
            assert all(self.replicas[key] == self.started[key] for key in self.replicas if key[1] != name), (
                "the daemon was resumed while other Deployments were down"
            )
        elif replicas and namespace != SHARED:  # the stages use the shared services
            assert all(self.replicas[key] == self.started[key] for key in self.replicas if key[0] == SHARED), (
                f"{name} was resumed while a shared service was down"
            )
        if remember:
            self.remembered[(namespace, name)] = remember
        elif replicas and remember is not None:
            self.remembered.pop((namespace, name), None)
        if replicas == 0:
            self.stopped.add((namespace, name))
        self.replicas[(namespace, name)] = replicas

    def suspend_cronjobs(self, namespace: str, **_: Any) -> None:
        for (ns, _name), state in self.cronjobs.items():
            if ns == namespace:
                state[1] = state[0] if state[1] is None else state[1]  # a rerun keeps the first answer
                state[0] = True

    def resume_cronjobs(self, namespace: str, **_: Any) -> None:
        for (ns, name), state in self.cronjobs.items():
            if ns == namespace and state[1] is not None:
                self.cronjobs[(ns, name)] = [state[1], None]

    def runs_in_flight(self, namespace: str) -> list[str]:
        assert self.replicas[(namespace, DAEMON_DEPLOYMENT)] == 0, "a run can still be launched: the daemon runs"
        assert self.replicas[(namespace, DAGSTER_WEBSERVER_SERVICE_NAME)], "the webserver is down; nothing to ask"
        return self.runs.get(namespace, [])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    return FakeCluster(monkeypatch)


@pytest.fixture
def backup(tmp_path: Path) -> Backup:
    return Backup(tmp_path)


def _restore_it(fake: FakeCluster, backup: Backup) -> bool:
    return restore(
        backup.directory,
        passphrase=PASSPHRASE,
        release="1.2.3",
        profile=backup.profile,
        stage_namespaces=STAGES,
        shared_namespace=SHARED,
        runs_in_flight=fake.runs_in_flight,
        environ=_ENVIRON,
    )


def _state(fake: FakeCluster) -> tuple[Any, ...]:
    """Everything a restore changes."""
    databases = {name: (db.owner, db.data) for name, db in fake.postgres.databases.items()}
    return (dict(fake.replicas), dict(fake.secrets), databases, dict(fake.s3.objects), dict(fake.cronjobs))


def test_every_client_is_stopped_for_the_restore_and_comes_back_with_the_replicas_it_had(
    fake: FakeCluster, backup: Backup
) -> None:
    _restore_it(fake, backup)

    # The Dagster and Superset Deployments and the shared database clients; not SeaweedFS (the upload goes through
    # it) or Traefik.
    assert fake.stopped == {key for key in fake.replicas if fake.is_client(*key)}
    assert fake.replicas == fake.started
    assert fake.remembered == {}


def test_the_whole_backup_is_restored(fake: FakeCluster, backup: Backup) -> None:
    fake.postgres.databases["polaris"] = Database("polaris", data=b"rows written after the backup")
    fake.s3.objects[_SILVER] = {"post-backup.csv": 5}

    _restore_it(fake, backup)

    assert {name: db.data for name, db in fake.postgres.databases.items()} == {
        "dagster_dev": b"dump of dagster_dev",
        "openmetadata_db": b"dump of openmetadata_db",
        "polaris": b"dump of polaris",
    }
    assert fake.s3.objects == {_SILVER: {"t/a.parquet": 4, "b.json": 3}}
    assert {"local-ca", "polaris-bootstrap-credentials"} <= {name for _, name in fake.secrets}


def test_cronjobs_are_suspended_while_the_data_is_replaced_and_come_back_as_they_were(
    fake: FakeCluster, backup: Backup
) -> None:
    kept_suspended = ("olf-prod", "reports")
    fake.cronjobs[kept_suspended] = [True, None]

    _restore_it(fake, backup)  # the fake fails any change made while a CronJob can start

    assert fake.cronjobs == {_ARCHIVE: [False, None], kept_suspended: [True, None]}


@pytest.mark.parametrize("namespace", ["olf-dev", "olf-prod"])
def test_a_run_in_flight_refuses_the_restore_and_leaves_everything_as_it_was(
    fake: FakeCluster, backup: Backup, namespace: str
) -> None:
    fake.runs = {namespace: ["run-7"]}
    fake.s3.objects[_SILVER] = {"post-backup.csv": 5}
    fake.postgres.databases["polaris"] = Database("polaris", data=b"live")
    fake.secrets[(SHARED, "local-ca")] = ("Opaque", {"live": "ca"})
    before = _state(fake)

    stage = "dev" if namespace == "olf-dev" else "prod"
    with pytest.raises(BackupError, match=rf"refusing to restore while Dagster runs are in flight \({stage}: run-7\)"):
        _restore_it(fake, backup)

    assert _state(fake) == before


def test_a_failed_step_leaves_the_workloads_down_and_a_rerun_restores_everything(
    fake: FakeCluster, backup: Backup
) -> None:
    fake.postgres.failing = True

    with pytest.raises(BackupError) as failure:
        _restore_it(fake, backup)

    message = str(failure.value)
    assert "boom" in message
    assert "olf-dev/superset (was 2)" in message
    assert "olf infra restore --from" in message
    assert fake.running_clients() == []
    assert fake.cronjobs[_ARCHIVE] == [True, False]  # still suspended, and it remembers it was not

    # The webservers are down now, so the rerun cannot ask them for runs; the fake fails if it does.
    fake.postgres.failing = False
    _restore_it(fake, backup)

    assert fake.replicas == fake.started
    assert fake.cronjobs == {_ARCHIVE: [False, None]}
    assert fake.postgres.databases["polaris"].data == b"dump of polaris"


def _restore_and_reconcile(fake: FakeCluster, backup: Backup) -> None:
    """What `olf infra restore` does around the reconcile `olf infra deploy`."""
    ca_restored = _restore_it(fake, backup)
    fake.ca.reconcile()
    if ca_restored:
        reissue_after_reconcile(STAGES, SHARED, _ENVIRON)


def _signers(fake: FakeCluster) -> dict[str, str | None]:
    ca = fake.ca
    return {name: ca.signer(ns, secret) for (ns, name), (secret, _issuer) in ca.certificates.items()}


def test_the_restored_ca_reaches_the_trust_configmap_the_certificates_and_the_consumers(
    fake: FakeCluster, backup: Backup
) -> None:
    _restore_and_reconcile(fake, backup)

    assert fake.ca.ca == "CERT"
    assert fake.ca.trust == {CA_CONFIGMAP: "CERT"}
    assert _signers(fake) == {
        "local-ca-probe": "CERT",
        "trino-tls": "CERT",
        "ingress-shim": "CERT",
        "not-ours": "letsencrypt",  # another issuer's Certificate is left alone
    }
    # Those that mount the CA or a re-issued Secret restart, once everything they load is the restored one.
    assert sorted(fake.ca.restarted) == [(SHARED, "dex"), (SHARED, "polaris"), (SHARED, "trino-coordinator")]
    assert all(fake.ca.restored_when_restarted.values())
    assert fake.replicas == fake.started


def test_a_backup_without_the_local_ca_leaves_the_trust_and_the_certificates_alone(
    fake: FakeCluster, backup: Backup
) -> None:
    backup.write(bundle=[SECRETS[0]], secrets=(SecretRef(SHARED, "polaris-bootstrap-credentials", ("password",)),))

    assert _restore_it(fake, backup) is False
    fake.ca.reconcile()

    assert fake.ca.jobs == {PUBLISH_JOB}
    assert fake.ca.trust == {CA_CONFIGMAP: "OLD-CA"}
    assert set(_signers(fake).values()) == {"OLD-CA", "letsencrypt"}
    assert fake.ca.restarted == []


def test_a_failed_reissue_is_reported_and_a_rerun_restores_the_ca_everywhere(fake: FakeCluster, backup: Backup) -> None:
    fake.ca.cert_manager_down = True

    with pytest.raises(BackupError, match=r"restored CA did not reach the platform: .*did not re-issue Secret"):
        _restore_and_reconcile(fake, backup)
    assert fake.ca.restarted == []  # nothing restarted onto a CA that its Certificates are not signed by

    fake.ca.cert_manager_down = False
    _restore_and_reconcile(fake, backup)

    assert fake.ca.trust == {CA_CONFIGMAP: "CERT"}
    assert set(_signers(fake).values()) == {"CERT", "letsencrypt"}
    assert _signers(fake)["not-ours"] == "letsencrypt"
    assert sorted(fake.ca.restarted) == [(SHARED, "dex"), (SHARED, "polaris"), (SHARED, "trino-coordinator")]
    assert all(fake.ca.restored_when_restarted.values())


def test_a_backup_that_fails_its_checks_changes_nothing(fake: FakeCluster, backup: Backup) -> None:
    (backup.directory / "postgres" / "polaris.dump").write_bytes(b"tampered")
    before = _state(fake)

    with pytest.raises(BackupError, match="does not match its checksum"):
        _restore_it(fake, backup)

    assert _state(fake) == before


def test_the_command_is_registered_and_names_its_source_option() -> None:
    env = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    result = CliRunner().invoke(app, ["infra", "restore", "--help"], env=env)

    assert result.exit_code == 0
    assert "--from" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)
