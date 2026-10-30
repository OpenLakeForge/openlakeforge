from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import e2e_cfg

from olf.infra import secrets_rotation
from olf.infra.e2e import _credential_preservation as preservation
from olf.infra.e2e._shell import E2EError

SAME = {"olf-system/openmetadata-admin/admin-password": "o" * 28, "olf-dev/superset-credentials/secret-key": "s" * 64}


class Cluster:
    """The Jobs in the cluster, as `kubectl` lists and deletes them, and a platform apply that recreates them."""

    def __init__(self, *, recreates: bool = True) -> None:
        self.jobs = {
            ("olf-system", "postgresql-credentials-abc"): "uid-1",
            ("olf-dev", "superset-credentials-def"): "uid-2",
            ("olf-system", "openmetadata-bootstrap-3"): "uid-4",
            ("olf-system", "openmetadata-credential-replication-1a"): "uid-5",
            ("olf-system", "dagster-daemon"): "uid-3",  # not a credential Job
        }
        self.recreates = recreates
        self.deleted: list[tuple[str, str]] = []
        self.applies = 0

    def kubectl(self, _cfg: object, args: list[str], **_kwargs: object) -> str:
        if args[:2] == ["get", "jobs"]:
            items = [{"metadata": {"namespace": ns, "name": name, "uid": uid}} for (ns, name), uid in self.jobs.items()]
            return json.dumps({"items": items})
        assert args[:2] == ["delete", "job"]
        key = (args[args.index("-n") + 1], args[2])
        del self.jobs[key]
        self.deleted.append(key)
        return ""

    def apply(self, _context: object, _args: list[str]) -> None:
        self.applies += 1
        if self.recreates:
            for ns, name in self.deleted:
                self.jobs[(ns, name)] = f"new-{name}"


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cluster: Cluster, after: dict[str, str]) -> None:
    reads = iter([SAME, after])
    monkeypatch.setattr(preservation, "load_provider_contracts_or_raise", lambda _cfg: {})
    monkeypatch.setattr(preservation, "scan_namespaces", lambda _cfg, _contracts: ("olf-system", "olf-dev"))
    monkeypatch.setattr(preservation, "migrated_credentials", lambda _cfg, _namespaces: next(reads))
    monkeypatch.setattr(preservation, "kubectl", cluster.kubectl)
    monkeypatch.setattr(secrets_rotation, "check_platform_state", lambda _ctx: None)
    monkeypatch.setattr(secrets_rotation, "builds_superset_image", lambda _ctx: False)
    monkeypatch.setattr(secrets_rotation, "apply_platform", cluster.apply)
    preservation.check_credential_preservation(replace(e2e_cfg(tmp_path), deployment=object()))


def test_unchanged_credentials_pass_once_the_credential_jobs_are_recreated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cluster = Cluster()

    _run(monkeypatch, tmp_path, cluster, after=SAME)

    assert cluster.applies == 1
    assert cluster.jobs[("olf-system", "dagster-daemon")] == "uid-3"
    assert ("olf-system", "openmetadata-credential-replication-1a") in cluster.deleted


def test_an_apply_that_leaves_the_jobs_deleted_fails_instead_of_passing_untested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(E2EError, match="did not recreate") as raised:
        _run(monkeypatch, tmp_path, Cluster(recreates=False), after=SAME)

    assert "olf-dev/superset-credentials-def" in str(raised.value)


def test_a_changed_or_lost_credential_fails_by_name_and_not_by_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    after = {"olf-system/openmetadata-admin/admin-password": "n" * 28}

    with pytest.raises(E2EError) as raised:
        _run(monkeypatch, tmp_path, Cluster(), after=after)

    message = str(raised.value)
    assert "olf-system/openmetadata-admin/admin-password" in message
    assert "olf-dev/superset-credentials/secret-key" in message
    assert "n" * 28 not in message and "o" * 28 not in message


def test_a_cluster_without_credential_jobs_is_an_error_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cluster = Cluster()
    cluster.jobs = {("olf-system", "dagster-daemon"): "uid-3"}

    with pytest.raises(E2EError, match="re-run nothing"):
        _run(monkeypatch, tmp_path, cluster, after=SAME)

    assert cluster.applies == 0


def test_the_drill_is_skipped_on_a_stack_with_a_locally_built_superset_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cluster = Cluster()
    monkeypatch.setattr(secrets_rotation, "builds_superset_image", lambda _ctx: True)
    monkeypatch.setattr(secrets_rotation, "apply_platform", cluster.apply)
    monkeypatch.setattr(preservation, "kubectl", cluster.kubectl)

    preservation.check_credential_preservation(replace(e2e_cfg(tmp_path), deployment=object()))

    assert cluster.applies == 0 and len(cluster.jobs) == 5


def test_the_drill_is_skipped_without_a_resolved_deployment(tmp_path: Path) -> None:
    preservation.check_credential_preservation(e2e_cfg(tmp_path))


def test_a_cluster_with_no_migrated_credential_is_an_error_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(preservation, "load_provider_contracts_or_raise", lambda _cfg: {})
    monkeypatch.setattr(preservation, "scan_namespaces", lambda _cfg, _contracts: ("olf-system",))
    monkeypatch.setattr(preservation, "migrated_credentials", lambda _cfg, _namespaces: {})
    monkeypatch.setattr(secrets_rotation, "builds_superset_image", lambda _ctx: False)

    with pytest.raises(E2EError, match="nothing to preserve"):
        preservation.check_credential_preservation(replace(e2e_cfg(tmp_path), deployment=object()))
