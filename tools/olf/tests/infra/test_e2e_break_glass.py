from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import e2e_cfg

from olf.infra.e2e import _break_glass as check


class _Response:
    status_code = 200


def test_the_record_is_read_with_the_runs_environment_not_the_operators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The records are written through the deployment's kubeconfig, so they must be read through it too (#559)."""
    environ = {"KUBECONFIG": str(tmp_path / "deployment.yaml")}
    cfg = replace(e2e_cfg(tmp_path), environ=environ)
    reads: list[dict[str, Any]] = []

    def open_session(cluster: Any, *_args: Any, run: Any, **_kw: Any) -> str:
        run(["kubectl", "port-forward"], env=cluster.env)
        return "bg-1"

    def recent(_kubectl: Any, _namespace: str, **kw: Any) -> list[dict[str, str]]:
        reads.append(kw)
        return [{"reason": check.REASON, "ended": "t", "user": "op", "duration_seconds": "3"}]

    monkeypatch.setattr(
        check,
        "load_provider_contracts_or_raise",
        lambda _cfg: {"shared": {"access": {"perimeter": {"enabled": True}}}},
    )
    monkeypatch.setattr(check, "kubectl", lambda _cfg, _args, **_kw: "ingress/a")
    monkeypatch.setattr(check.break_glass, "open_session", open_session)
    monkeypatch.setattr(check.break_glass, "recent", recent)
    monkeypatch.setattr(check.ProcessRunner, "popen", lambda *_a, **_kw: _Process())
    monkeypatch.setattr(check.requests, "get", lambda *_a, **_kw: _Response())

    check.check_break_glass(cfg)

    (read,) = reads
    assert read["env"] == environ
    assert read["context"] == cfg.kube_context


class _Process:
    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0
