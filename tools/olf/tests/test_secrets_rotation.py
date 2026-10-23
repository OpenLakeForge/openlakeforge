from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from olf import secrets_rotation
from olf.cli import app

SECRET_VALUE = "hunter2-do-not-print"


class FakeKubectl:
    """Records calls; `get` answers the Secret's identity the way the real jsonpath would."""

    def __init__(self, identities: list[str]) -> None:
        self.identities = identities
        self.calls: list[tuple[Any, ...]] = []

    def get(self, resource: str, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["output"] == "jsonpath={.metadata.uid}/{.metadata.resourceVersion}"
        self.calls.append(("get", resource, kwargs["name"]))
        return SimpleNamespace(stdout=self.identities.pop(0))

    def delete(self, resource: str, name: str, **_: Any) -> None:
        self.calls.append(("delete", resource, name))

    def rollout_restart(self, resource: str, **kwargs: Any) -> None:
        self.calls.append(("restart", resource, kwargs["selector"]))

    def rollout_status(self, resource: str, **kwargs: Any) -> None:
        self.calls.append(("status", resource, kwargs["selector"]))


def context(tmp_path: Path) -> Any:
    return SimpleNamespace(
        kube_context="kind-test",
        shared_namespace="olf-system",
        paths=SimpleNamespace(kubeconfig_path=tmp_path / "kubeconfig"),
        command_env=lambda base: dict(base),
    )


def test_a_client_is_deleted_regenerated_by_the_platform_apply_and_its_consumers_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TF_CLI_ARGS_apply", raising=False)
    kubectl = FakeKubectl(["uid-1/10", "uid-2/20"])
    applied: list[dict[str, str]] = []

    rotation = secrets_rotation.rotate_client(
        context(tmp_path),
        "dex-client-perimeter",
        kubectl=kubectl,
        platform_apply=lambda _ctx, env: applied.append(dict(env)),
    )

    job = "module.dex[0].kubernetes_job_v1.credentials"
    assert applied == [{"TF_CLI_ARGS_apply": f"-target={job} -replace={job}"}]
    assert kubectl.calls == [
        ("get", "secret", "dex-client-perimeter"),
        ("delete", "secret", "dex-client-perimeter"),
        ("get", "secret", "dex-client-perimeter"),
        ("restart", "deployment/dex", None),
        ("restart", "deployment", "openlakeforge.io/component=perimeter"),
        ("status", "deployment/dex", None),
        ("status", "deployment", "openlakeforge.io/component=perimeter"),
    ]
    assert rotation.secret == "dex-client-perimeter"


def test_trino_restarts_the_coordinator(tmp_path: Path) -> None:
    kubectl = FakeKubectl(["a/1", "b/2"])
    secrets_rotation.rotate_client(
        context(tmp_path), "dex-client-trino", kubectl=kubectl, platform_apply=lambda *_: None
    )
    assert ("restart", "deployment/trino-coordinator", None) in kubectl.calls


def test_a_secret_the_apply_did_not_recreate_fails_before_any_restart(tmp_path: Path) -> None:
    for identities in (["uid-1/10", "uid-1/10"], ["uid-1/10", ""]):
        kubectl = FakeKubectl(identities)
        with pytest.raises(secrets_rotation.RotationError, match="was not regenerated"):
            secrets_rotation.rotate_client(
                context(tmp_path), "dex-client-perimeter", kubectl=kubectl, platform_apply=lambda *_: None
            )
        assert not [c for c in kubectl.calls if c[0] == "restart"]


def test_an_unknown_target_lists_the_supported_ones(tmp_path: Path) -> None:
    with pytest.raises(secrets_rotation.RotationError) as err:
        secrets_rotation.rotate_client(context(tmp_path), "dex-client-superset", kubectl=FakeKubectl([]))
    for target in secrets_rotation.TARGETS:
        assert target in str(err.value)


def test_the_command_rejects_an_unknown_target_and_prints_a_summary_with_no_secret_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = CliRunner()
    bad = runner.invoke(app, ["secrets", "rotate", "nope"])
    assert bad.exit_code != 0
    assert "dex-client-perimeter" in bad.output

    from olf.commands import secrets as command

    monkeypatch.setattr(command, "deployment_context", lambda *a, **k: context(tmp_path))
    kubectl = FakeKubectl(["u/1", "u/2"])
    monkeypatch.setattr("olf.deployment.engine.Toolkit.default", lambda **_: SimpleNamespace(kubectl=kubectl))
    real = secrets_rotation.rotate_client
    monkeypatch.setattr(
        secrets_rotation,
        "rotate_client",
        lambda ctx, target, *, kubectl: real(ctx, target, kubectl=kubectl, platform_apply=lambda *_: None),
    )

    good = runner.invoke(app, ["secrets", "rotate", "dex-client-perimeter"])

    assert good.exit_code == 0, good.output
    out = good.output + capsys.readouterr().out
    assert "Rotated Secret 'dex-client-perimeter'" in out
    assert "deployment/dex" in out
    assert SECRET_VALUE not in out


def test_a_profile_file_selects_the_profile_driven_context_that_leaves_user_deployments_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.commands import secrets as command

    profile_context = context(tmp_path)
    profile_context.manage_user_deployments = False
    seen: list[str] = []
    monkeypatch.setattr(
        command, "deployment_context_for_profile", lambda path, **_: seen.append(path) or profile_context
    )
    monkeypatch.setattr(command, "deployment_context", lambda *a, **k: pytest.fail("the project-root context"))
    kubectl = FakeKubectl(["u/1", "u/2"])
    monkeypatch.setattr("olf.deployment.engine.Toolkit.default", lambda **_: SimpleNamespace(kubectl=kubectl))
    applied: list[Any] = []
    real = secrets_rotation.rotate_client
    monkeypatch.setattr(
        secrets_rotation,
        "rotate_client",
        lambda ctx, target, *, kubectl: real(
            ctx, target, kubectl=kubectl, platform_apply=lambda c, _env: applied.append(c)
        ),
    )

    result = CliRunner().invoke(app, ["secrets", "rotate", "dex-client-perimeter", "-f", "profile.yaml"])

    assert result.exit_code == 0, result.output
    assert seen == ["profile.yaml"]
    assert [c.manage_user_deployments for c in applied] == [False]


def test_github_app_delegates_to_the_connect_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from olf import github_app
    from olf.commands import secrets as command

    monkeypatch.setattr(command, "deployment_context", lambda *a, **k: context(tmp_path))
    seen: list[bool] = []
    monkeypatch.setattr(github_app, "connect_github", lambda _ctx, *, no_browser: seen.append(no_browser) or "url")
    kubectl = FakeKubectl([])
    monkeypatch.setattr("olf.deployment.engine.Toolkit.default", lambda **_: SimpleNamespace(kubectl=kubectl))

    result = CliRunner().invoke(app, ["secrets", "rotate", "github-app", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert seen == [True]
    assert ("restart", "deployment/dex", None) in kubectl.calls
    assert ("status", "deployment/dex", None) in kubectl.calls


def test_the_replace_flag_is_in_the_process_environment_only_while_the_platform_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from olf.deployment import engine

    seen: list[str | None] = []

    class FakeEngine:
        def __init__(self, _provider: object) -> None: ...

        def deploy(self, _phase: object) -> None:
            seen.append(os.environ.get("TF_CLI_ARGS_apply"))

    monkeypatch.delenv("TF_CLI_ARGS_apply", raising=False)
    monkeypatch.setattr(engine, "DeploymentEngine", FakeEngine)
    monkeypatch.setattr(engine, "build_provider", lambda *a, **k: object())
    monkeypatch.setattr(engine.Toolkit, "default", classmethod(lambda cls, **_: object()))

    secrets_rotation.apply_platform(context(tmp_path), {"TF_CLI_ARGS_apply": "-replace=x"})

    assert seen == ["-replace=x"]
    assert "TF_CLI_ARGS_apply" not in os.environ


def test_the_drill_skips_without_a_resolved_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.e2e import _rotation

    contracts = {"shared": {"access": {"perimeter": {"enabled": True}}, "identity": {"adapter": "dex"}}}
    monkeypatch.setattr(_rotation, "load_provider_contracts_or_raise", lambda _cfg: contracts)
    called: list[str] = []
    monkeypatch.setattr(_rotation.secrets_rotation, "rotate_client", lambda *a, **k: called.append("rotated"))

    _rotation.check_secret_rotation(SimpleNamespace(deployment=None))  # type: ignore[arg-type]

    assert called == []
