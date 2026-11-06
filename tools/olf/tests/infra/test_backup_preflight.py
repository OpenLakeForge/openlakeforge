"""The checks `olf infra restore` makes before it changes anything (#911): what is refused, and what passes."""

from __future__ import annotations

from pathlib import Path

import pytest
from _backup_fixture import PASSPHRASE, PROFILE_TEXT, SHARED, STAGES, Backup

from olf.infra import k8s
from olf.infra.backup import BackupError
from olf.infra.backup._manifest import SecretRef
from olf.infra.backup._preflight import preflight


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """The cluster as preflight sees it: PostgreSQL and Polaris, until `deployed` is set False."""
    state = {"deployed": True}
    monkeypatch.setattr(k8s, "resource_exists", lambda *_a, **_k: state["deployed"])
    monkeypatch.setattr(k8s, "deployment_replica_counts", lambda *_a, **_k: {"polaris": 1} if state["deployed"] else {})
    return state


@pytest.fixture
def backup(tmp_path: Path) -> Backup:
    return Backup(tmp_path)


def _preflight(backup: Backup, *, release: str = "1.2.3", passphrase: str = PASSPHRASE) -> None:
    preflight(
        backup.directory,
        passphrase=passphrase,
        release=release,
        profile=backup.profile,
        stage_namespaces=STAGES,
        shared_namespace=SHARED,
        environ={},
    )


def test_a_backup_of_this_release_profile_and_stages_on_a_deployed_platform_passes(
    deployed: dict[str, bool], backup: Backup
) -> None:
    manifest, secrets = preflight(
        backup.directory,
        passphrase=PASSPHRASE,
        release="1.2.3",
        profile=backup.profile,
        stage_namespaces=STAGES,
        shared_namespace=SHARED,
        environ={},
    )

    assert [database.name for database in manifest.databases] == ["dagster_dev", "openmetadata_db", "polaris"]
    assert [secret["name"] for secret in secrets] == ["polaris-bootstrap-credentials", "local-ca"]


def test_a_backup_from_another_release_is_refused(deployed: dict[str, bool], backup: Backup) -> None:
    with pytest.raises(BackupError, match="release 1.2.3.*olf is 1.2.4"):
        _preflight(backup, release="1.2.4")


def test_a_backup_from_another_provider_is_refused(deployed: dict[str, bool], backup: Backup) -> None:
    backup.write(provider="aws")

    with pytest.raises(BackupError, match="only the local provider"):
        _preflight(backup)


def test_a_changed_profile_is_refused(deployed: dict[str, bool], backup: Backup) -> None:
    backup.profile.write_text(PROFILE_TEXT + "# edited\n")

    with pytest.raises(BackupError, match="differs from the Deployment Profile"):
        _preflight(backup)


def test_a_different_stage_set_is_refused(deployed: dict[str, bool], backup: Backup) -> None:
    backup.write(stages=("dev",))

    with pytest.raises(BackupError, match="holds stages"):
        _preflight(backup)


def test_a_tampered_file_is_refused(deployed: dict[str, bool], backup: Backup) -> None:
    (backup.directory / "postgres" / "polaris.dump").write_bytes(b"tampered")

    with pytest.raises(BackupError, match="polaris.dump does not match its checksum"):
        _preflight(backup)


def test_a_wrong_passphrase_is_refused_without_echoing_it(deployed: dict[str, bool], backup: Backup) -> None:
    with pytest.raises(BackupError, match="passphrase is wrong") as failure:
        _preflight(backup, passphrase="wrong guess")

    assert "wrong guess" not in str(failure.value)


def test_secrets_that_are_not_the_manifests_are_refused(deployed: dict[str, bool], backup: Backup) -> None:
    backup.write(bundle=[{"namespace": "kube-system", "name": "x", "type": "Opaque", "data": {}}])

    with pytest.raises(BackupError, match="does not hold the Secrets manifest.json lists"):
        _preflight(backup)


def test_secrets_for_a_namespace_the_deployment_does_not_own_are_refused(
    deployed: dict[str, bool], backup: Backup
) -> None:
    stray = [{"namespace": "kube-system", "name": "x", "type": "Opaque", "data": {}}]
    backup.write(bundle=stray, secrets=(SecretRef("kube-system", "x", ()),))

    with pytest.raises(BackupError, match="namespaces this deployment does not own"):
        _preflight(backup)


def test_a_platform_that_is_not_deployed_is_refused(deployed: dict[str, bool], backup: Backup) -> None:
    deployed["deployed"] = False

    with pytest.raises(BackupError, match="olf infra deploy --phase platform"):
        _preflight(backup)
