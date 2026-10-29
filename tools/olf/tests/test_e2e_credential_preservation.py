from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import e2e_cfg

from olf import secrets_rotation
from olf.e2e import _credential_preservation as preservation
from olf.e2e._shell import E2EError

SAME = {"olf-system/openmetadata-admin/admin-password": "o" * 28, "olf-dev/superset-credentials/secret-key": "s" * 64}


def _drill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, after: dict[str, str]) -> list[str]:
    reads = iter([SAME, after])
    steps: list[str] = []
    monkeypatch.setattr(preservation, "load_provider_contracts_or_raise", lambda _cfg: {})
    monkeypatch.setattr(preservation, "scan_namespaces", lambda _cfg, _contracts: ("olf-system", "olf-dev"))
    monkeypatch.setattr(preservation, "migrated_credentials", lambda _cfg, _namespaces: next(reads))
    monkeypatch.setattr(preservation, "kubectl", lambda _cfg, args, **_kw: steps.append("delete " + args[-2]))
    monkeypatch.setattr(secrets_rotation, "check_platform_state", lambda _ctx: steps.append("check"))
    monkeypatch.setattr(secrets_rotation, "apply_platform", lambda _ctx, _args: steps.append("apply"))
    preservation.check_credential_preservation(replace(e2e_cfg(tmp_path), deployment=object()))
    return steps


def test_unchanged_credentials_pass_after_the_jobs_are_deleted_and_the_platform_is_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    steps = _drill(monkeypatch, tmp_path, after=SAME)

    assert [step.split()[0] for step in steps] == ["check", "delete", "apply"]


def test_a_changed_or_lost_credential_fails_by_name_and_not_by_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    after = {"olf-system/openmetadata-admin/admin-password": "n" * 28}

    with pytest.raises(E2EError) as raised:
        _drill(monkeypatch, tmp_path, after=after)

    message = str(raised.value)
    assert "olf-system/openmetadata-admin/admin-password" in message
    assert "olf-dev/superset-credentials/secret-key" in message
    assert "n" * 28 not in message and "o" * 28 not in message


def test_the_drill_is_skipped_without_a_resolved_deployment(tmp_path: Path) -> None:
    preservation.check_credential_preservation(e2e_cfg(tmp_path))


def test_a_cluster_with_no_migrated_credential_is_an_error_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(preservation, "load_provider_contracts_or_raise", lambda _cfg: {})
    monkeypatch.setattr(preservation, "scan_namespaces", lambda _cfg, _contracts: ("olf-system",))
    monkeypatch.setattr(preservation, "migrated_credentials", lambda _cfg, _namespaces: {})
    with pytest.raises(E2EError, match="nothing to preserve"):
        preservation.check_credential_preservation(replace(e2e_cfg(tmp_path), deployment=object()))
