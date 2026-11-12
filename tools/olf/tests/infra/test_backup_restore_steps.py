"""The steps of `olf infra restore` (#911): what Secrets, databases and buckets hold afterwards."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from _backup_fixture import PASSPHRASE, SHARED, Backup, b64
from _polaris_fake import Cluster, FakePolaris, serve
from _restore_fakes import Database, DataPlane

from olf.common.tooling.kubectl import Kubectl
from olf.infra import k8s
from olf.infra.backup import BackupError
from olf.infra.backup._bundle import decrypt_secrets
from olf.infra.backup._manifest import DatabaseDump, Manifest, SecretRef, load_manifest
from olf.infra.backup._restore import _replace_secrets, _restore_database, _upload_objects
from olf.infra.polaris import PolarisClient, PolarisConfig
from olf.infra.polaris_bootstrap import Credential, ensure_credential

_ENVIRON = {"KUBE_CONTEXT": "kind-test"}
_SHARED = Credential("trino-dev", "polaris-dev-trino-creds", "TRINO_ID", "TRINO_SECRET", SHARED)
_SILVER = "lakehouse-dev-silver"


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> DataPlane:
    return DataPlane(monkeypatch)


@pytest.fixture
def backup(tmp_path: Path) -> Backup:
    return Backup(tmp_path)


def _manifest(backup: Backup) -> Manifest:
    return load_manifest(backup.directory)


def _secrets(backup: Backup) -> list[dict[str, Any]]:
    return decrypt_secrets((backup.directory / "secrets.enc").read_bytes(), PASSPHRASE)


def _restore_databases(backup: Backup) -> None:
    for dump in _manifest(backup).databases:
        _restore_database(backup.directory, dump, SHARED, _ENVIRON)


def test_secrets_come_back_with_their_type_and_exact_keys_in_place_of_what_was_there(
    plane: DataPlane, backup: Backup
) -> None:
    plane.secrets[(SHARED, "local-ca")] = ("Opaque", {"stale": "from a fresh deploy"})

    _replace_secrets(_manifest(backup).secrets, _secrets(backup), _ENVIRON)

    assert plane.secrets == {
        (SHARED, "polaris-bootstrap-credentials"): ("Opaque", {"password": "pw"}),
        (SHARED, "local-ca"): ("kubernetes.io/tls", {"tls.crt": "CERT", "tls.key": "KEY"}),
    }


def test_a_secret_that_cannot_be_written_fails_without_a_value_from_the_bundle(
    plane: DataPlane, backup: Backup
) -> None:
    backup.write(
        bundle=[{"namespace": SHARED, "name": "local-ca", "type": "Opaque", "data": {"key": b64("hunter2-value")}}],
        secrets=(SecretRef(SHARED, "local-ca", ("key",)),),
    )
    plane.secret_error = "kubectl create failed:"

    with pytest.raises(BackupError, match="could not write Secret olf-system/local-ca") as failure:
        _replace_secrets(_manifest(backup).secrets, _secrets(backup), _ENVIRON)

    assert failure.value.__suppress_context__  # the kubectl error, which echoes the data, is not shown
    assert "hunter2-value" not in str(failure.value)


def test_each_database_is_recreated_owned_by_its_role_with_its_grants_and_loaded_with_its_dump(
    plane: DataPlane, backup: Backup
) -> None:
    plane.postgres.databases["polaris"] = Database("stale-owner", data=b"rows written after the backup")
    plane.postgres.databases["unrelated"] = Database("someone", data=b"untouched")

    _restore_databases(backup)

    restored = {
        name: (database.owner, database.connect, database.privileges, database.schema_owner, database.data)
        for name, database in plane.postgres.databases.items()
    }
    assert restored == {
        "dagster_dev": ("dagster_dev", {"dagster_dev"}, {"dagster_dev"}, "dagster_dev", b"dump of dagster_dev"),
        "openmetadata_db": (
            "openmetadata_user",
            {"openmetadata_user"},
            {"openmetadata_user"},
            "openmetadata_user",
            b"dump of openmetadata_db",
        ),
        "polaris": ("polaris", {"polaris"}, {"polaris"}, "polaris", b"dump of polaris"),
        "unrelated": ("someone", {"PUBLIC"}, set(), "postgres", b"untouched"),
    }


def test_a_name_that_could_carry_sql_is_refused_and_no_database_changes(plane: DataPlane, backup: Backup) -> None:
    plane.postgres.databases["polaris"] = Database("polaris", data=b"live")
    backup.write(databases=(DatabaseDump('x"; DROP DATABASE polaris; --', "o", "postgres/polaris.dump", "x", 4),))

    with pytest.raises(BackupError, match="only letters, digits and _"):
        _restore_databases(backup)

    assert plane.postgres.databases == {"polaris": Database("polaris", data=b"live")}


def test_each_bucket_ends_up_equal_to_the_backup_so_objects_written_after_it_are_gone(
    plane: DataPlane, backup: Backup
) -> None:
    plane.s3.objects[_SILVER] = {"post-backup.csv": 5, "t/new.parquet": 9, "b.json": 99}

    _upload_objects(backup.directory, _manifest(backup), SHARED, _ENVIRON)

    assert plane.s3.objects == {_SILVER: {"t/a.parquet": 4, "b.json": 3}}


def test_an_object_that_cannot_be_deleted_fails_the_restore_naming_it(plane: DataPlane, backup: Backup) -> None:
    plane.s3.objects[_SILVER] = {"post-backup.csv": 5}
    plane.s3.undeletable = {"post-backup.csv"}

    with pytest.raises(BackupError, match=r"does not match its inventory.*post-backup\.csv"):
        _upload_objects(backup.directory, _manifest(backup), SHARED, _ENVIRON)


def test_an_object_that_arrives_with_another_size_fails_the_restore(plane: DataPlane, backup: Backup) -> None:
    plane.s3.shrink = True

    with pytest.raises(BackupError, match="does not match its inventory"):
        _upload_objects(backup.directory, _manifest(backup), SHARED, _ENVIRON)


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Cluster:
    """Secrets and ConfigMaps answering olf's kubectl calls, as in test_polaris_bootstrap."""
    cluster = Cluster(tmp_path / "cluster.json")
    resolver = Mock()
    resolver.resolve.return_value = tmp_path / "kubectl"
    monkeypatch.setattr(k8s, "ProcessRunner", lambda: cluster)
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda *_a: "kubectl")
    monkeypatch.setattr(k8s.Toolkit, "default", lambda **_k: SimpleNamespace(kubectl=Kubectl(cluster, resolver)))
    return cluster


def test_restored_polaris_state_and_secrets_are_kept_by_the_bootstrap(cluster: Cluster) -> None:
    fake = FakePolaris()
    with serve(fake) as url:
        client = PolarisClient(PolarisConfig(url, "", "root", "root-secret-value"))
        client.login()
        # The restored `polaris` database knows the principal; the restored Secret holds the credential issued for it.
        issued = client.management("POST", "/principals", payload={"name": _SHARED.principal}, ok_statuses=(201,))
        credentials = issued["credentials"]
        secret = {
            "namespace": _SHARED.namespace,
            "name": _SHARED.secret,
            "type": "Opaque",
            "data": {
                _SHARED.client_id_key: b64(credentials["clientId"]),
                _SHARED.client_secret_key: b64(credentials["clientSecret"]),
            },
        }
        cluster.put("secret", _SHARED.namespace, _SHARED.secret, {"stale": "from a fresh deploy"})
        issued_before, writes_before = fake.issued, list(fake.writes)

        ref = SecretRef(_SHARED.namespace, _SHARED.secret, (_SHARED.client_id_key, _SHARED.client_secret_key))
        _replace_secrets((ref,), [secret], _ENVIRON)
        ensure_credential(client, _SHARED, environ=_ENVIRON)

    assert cluster.load()[f"secret/{_SHARED.namespace}/{_SHARED.secret}"] == {
        _SHARED.client_id_key: credentials["clientId"],
        _SHARED.client_secret_key: credentials["clientSecret"],
    }
    assert fake.issued == issued_before
    assert fake.writes == writes_before
    assert credentials["clientSecret"] not in json.dumps(cluster.argv)


def test_a_secret_its_controller_recreates_on_delete_still_gets_the_backed_up_keys(
    plane: DataPlane, backup: Backup
) -> None:
    # cert-manager re-creates `local-ca`, its CA Certificate's Secret, the moment restore deletes it, so the
    # create that follows finds it there (main run 43179953271: "could not write Secret olf-system/local-ca").
    plane.recreated_on_delete[(SHARED, "local-ca")] = ("kubernetes.io/tls", {"tls.crt": "NEW", "tls.key": "NEW"})

    _replace_secrets(_manifest(backup).secrets, _secrets(backup), _ENVIRON)

    assert plane.secrets[(SHARED, "local-ca")] == ("kubernetes.io/tls", {"tls.crt": "CERT", "tls.key": "KEY"})
