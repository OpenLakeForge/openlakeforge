from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from olf import secrets_rotation
from olf.cli import app

SECRET_VALUE = "hunter2-do-not-print"
CLIENTS = [f"dex-client-{c}" for c in ("perimeter", "trino", "superset", "openmetadata")]
KEY_OUTPUT = "go-template=" + secrets_rotation.KEY_PRESENT
IDENTITY_OUTPUT = "jsonpath={.metadata.uid}/{.metadata.resourceVersion}"


class FakeKubectl:
    """A cluster of Secrets: identity by name, and which of them have the client-secret key.

    Only the two read-only views the command uses are answered, so a test also shows no value is requested."""

    def __init__(self, secrets: dict[str, str] | None = None, *, without_key: tuple[str, ...] = ()) -> None:
        self.secrets = dict(secrets if secrets is not None else {name: f"uid-{name}/1" for name in CLIENTS})
        self.without_key = without_key
        self.calls: list[tuple[Any, ...]] = []

    def get(self, resource: str, **kwargs: Any) -> SimpleNamespace:
        name = kwargs["name"]
        if kwargs["output"] == KEY_OUTPUT:
            has_key = name in self.secrets and name not in self.without_key
            return SimpleNamespace(stdout="yes" if has_key else "")
        assert kwargs["output"] == IDENTITY_OUTPUT
        self.calls.append(("get", resource, name))
        return SimpleNamespace(stdout=self.secrets.get(name, ""))

    def delete(self, resource: str, name: str, **_: Any) -> None:
        self.calls.append(("delete", resource, name))
        self.secrets.pop(name, None)

    def rollout_restart(self, resource: str, **kwargs: Any) -> None:
        self.calls.append(("restart", resource, kwargs["selector"]))

    def rollout_status(self, resource: str, **kwargs: Any) -> None:
        self.calls.append(("status", resource, kwargs["selector"]))

    def deletes(self) -> list[Any]:
        return [c for c in self.calls if c[0] == "delete"]


def context(tmp_path: Path, *, provider: str = "local", issuer: str = "github") -> Any:
    return SimpleNamespace(
        kube_context="kind-test",
        shared_namespace="olf-system",
        paths=SimpleNamespace(kubeconfig_path=tmp_path / "kubeconfig"),
        command_env=lambda base: dict(base),
        provider=SimpleNamespace(value=provider),
        topology=SimpleNamespace(identity=SimpleNamespace(issuer=issuer)),
        manage_user_deployments=True,
    )


def apply_that_regenerates(kubectl: FakeKubectl, target: str, applied: list[Any] | None = None) -> Any:
    def apply(_ctx: Any, args: list[str]) -> None:
        kubectl.secrets[target] = f"uid-new/2-{len(kubectl.calls)}"
        if applied is not None:
            applied.append(list(args))

    return apply


def rotate(tmp_path: Path, kubectl: FakeKubectl, target: str = "dex-client-perimeter", **kwargs: Any) -> Any:
    kwargs.setdefault("platform_check", lambda _ctx: None)
    kwargs.setdefault("platform_apply", apply_that_regenerates(kubectl, target))
    return secrets_rotation.rotate_client(context(tmp_path), target, kubectl=kubectl, **kwargs)


def test_a_client_is_deleted_regenerated_by_a_targeted_apply_and_its_consumers_restarted(tmp_path: Path) -> None:
    kubectl = FakeKubectl()
    applied: list[Any] = []

    apply = apply_that_regenerates(kubectl, "dex-client-perimeter", applied)
    rotation = rotate(tmp_path, kubectl, platform_apply=apply)

    job = "module.dex[0].kubernetes_job_v1.credentials"
    assert applied == [[f"-target={job}", f"-replace={job}"]]
    assert [c for c in kubectl.calls if c[0] != "get"] == [
        ("delete", "secret", "dex-client-perimeter"),
        ("restart", "deployment/dex", None),
        ("restart", "deployment", "openlakeforge.io/component=perimeter"),
        ("status", "deployment/dex", None),
        ("status", "deployment", "openlakeforge.io/component=perimeter"),
    ]
    assert rotation.secret == "dex-client-perimeter"


def test_trino_restarts_the_coordinator(tmp_path: Path) -> None:
    kubectl = FakeKubectl()
    rotate(tmp_path, kubectl, "dex-client-trino")
    assert ("restart", "deployment/trino-coordinator", None) in kubectl.calls


def test_a_drifted_platform_state_refuses_before_anything_is_deleted(tmp_path: Path) -> None:
    kubectl = FakeKubectl()

    def drifted(_ctx: Any) -> None:
        raise secrets_rotation.RotationError("platform state has drifted; run olf deploy first")

    with pytest.raises(secrets_rotation.RotationError, match="drifted; run olf deploy first"):
        rotate(tmp_path, kubectl, platform_check=drifted)

    assert kubectl.deletes() == []


def test_a_target_without_the_client_secret_key_refuses_without_reading_a_value(tmp_path: Path) -> None:
    kubectl = FakeKubectl(without_key=("dex-client-perimeter",))

    with pytest.raises(secrets_rotation.RotationError, match="lacks the key client-secret"):
        rotate(tmp_path, kubectl)

    assert kubectl.deletes() == []
    assert secrets_rotation.KEY_PRESENT.count("yes") == 1 and "client-secret" in secrets_rotation.KEY_PRESENT


@pytest.mark.parametrize("missing", ["dex-client-trino", "dex-client-superset", "dex-client-openmetadata"])
def test_a_missing_sibling_client_refuses_so_the_shared_job_regenerates_only_the_target(
    tmp_path: Path, missing: str
) -> None:
    kubectl = FakeKubectl({name: f"uid-{name}/1" for name in CLIENTS if name != missing})

    with pytest.raises(secrets_rotation.RotationError, match=f"{missing}.*run olf deploy first"):
        rotate(tmp_path, kubectl)

    assert kubectl.deletes() == []


def test_a_secret_the_apply_did_not_recreate_fails_before_any_restart(tmp_path: Path) -> None:
    kubectl = FakeKubectl()
    with pytest.raises(secrets_rotation.RotationError, match="was not regenerated"):
        rotate(tmp_path, kubectl, platform_apply=lambda *_: None)
    assert not [c for c in kubectl.calls if c[0] == "restart"]


def test_a_provider_or_issuer_other_than_local_github_refuses_before_anything_is_touched(tmp_path: Path) -> None:
    for kwargs in ({"provider": "aws"}, {"issuer": "external"}):
        kubectl = FakeKubectl()
        with pytest.raises(secrets_rotation.RotationError, match="provider: local with identity.issuer: github"):
            secrets_rotation.rotate_client(
                context(tmp_path, **kwargs),
                "dex-client-perimeter",
                kubectl=kubectl,
                platform_check=lambda _ctx: None,
                platform_apply=lambda *_: None,
            )
        assert kubectl.calls == []


def test_an_unknown_target_lists_the_supported_ones(tmp_path: Path) -> None:
    with pytest.raises(secrets_rotation.RotationError) as err:
        rotate(tmp_path, FakeKubectl(), "dex-client-superset")
    for target in secrets_rotation.TARGETS:
        assert target in str(err.value)


def command_with_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl: FakeKubectl, **ctx: Any) -> Any:
    from olf.commands import secrets as command

    cluster = context(tmp_path, **ctx)
    monkeypatch.setattr(command, "deployment_context", lambda *a, **k: cluster)
    monkeypatch.setattr("olf.deployment.engine.Toolkit.default", lambda **_: SimpleNamespace(kubectl=kubectl))
    real = secrets_rotation.rotate_client
    monkeypatch.setattr(
        secrets_rotation,
        "rotate_client",
        lambda c, target, *, kubectl: real(
            c,
            target,
            kubectl=kubectl,
            platform_check=lambda _c: None,
            platform_apply=apply_that_regenerates(kubectl, target),
        ),
    )
    monkeypatch.setattr(secrets_rotation, "portal_url", lambda _c: "https://olf.localhost")
    return command


def test_the_command_rejects_an_unknown_target(tmp_path: Path) -> None:
    bad = CliRunner().invoke(app, ["secrets", "rotate", "nope"])
    assert bad.exit_code != 0
    assert "dex-client-perimeter" in bad.output


def test_the_command_summarises_and_asks_to_confirm_by_signing_in_with_no_secret_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command_with_fakes(monkeypatch, tmp_path, FakeKubectl())

    good = CliRunner().invoke(app, ["secrets", "rotate", "dex-client-perimeter"])

    assert good.exit_code == 0, good.output
    out = good.output + capsys.readouterr().out
    assert "Rotated Secret 'dex-client-perimeter'" in out
    assert "deployment/dex" in out
    assert "Confirm: sign in at https://olf.localhost." in out
    assert "Nothing else to do" not in out
    assert SECRET_VALUE not in out


def test_the_command_refuses_a_profile_that_is_not_local_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubectl = FakeKubectl()
    command_with_fakes(monkeypatch, tmp_path, kubectl, issuer="external")

    result = CliRunner().invoke(app, ["secrets", "rotate", "dex-client-perimeter"])

    assert result.exit_code != 0
    assert "issuer: github" in result.output
    assert kubectl.calls == []


def test_a_profile_file_selects_the_profile_driven_context_that_leaves_user_deployments_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.commands import secrets as command

    kubectl = FakeKubectl()
    command_with_fakes(monkeypatch, tmp_path, kubectl)
    profile_context = context(tmp_path)
    profile_context.manage_user_deployments = False
    seen: list[str] = []
    monkeypatch.setattr(
        command, "deployment_context_for_profile", lambda path, **_: seen.append(path) or profile_context
    )
    monkeypatch.setattr(command, "deployment_context", lambda *a, **k: pytest.fail("the project-root context"))
    contexts: list[Any] = []
    monkeypatch.setattr(
        secrets_rotation,
        "rotate_client",
        lambda c, target, *, kubectl: contexts.append(c) or secrets_rotation.Rotation(target, "olf-system", ()),
    )

    result = CliRunner().invoke(app, ["secrets", "rotate", "dex-client-perimeter", "-f", "profile.yaml"])

    assert result.exit_code == 0, result.output
    assert seen == ["profile.yaml"]
    assert [c.manage_user_deployments for c in contexts] == [False]


def test_github_app_delegates_to_the_connect_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from olf import github_app

    kubectl = FakeKubectl()
    command_with_fakes(monkeypatch, tmp_path, kubectl)
    seen: list[bool] = []
    monkeypatch.setattr(github_app, "connect_github", lambda _ctx, *, no_browser: seen.append(no_browser) or "url")

    result = CliRunner().invoke(app, ["secrets", "rotate", "github-app", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert seen == [True]
    assert ("restart", "deployment/dex", None) in kubectl.calls
    assert ("status", "deployment/dex", None) in kubectl.calls


def test_the_apply_is_the_targeted_one_not_the_platform_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.deployment.local import platform

    provider = SimpleNamespace(config="config", tools="tools", env={"K": "v"})
    monkeypatch.setattr(secrets_rotation, "_local_provider", lambda _ctx: provider)
    seen: list[Any] = []
    monkeypatch.setattr(platform, "apply_targets", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(platform, "platform_up", lambda *a, **k: pytest.fail("the full platform path"))

    secrets_rotation.apply_platform(context(tmp_path), secrets_rotation.APPLY_ARGS)

    assert seen == [(("config", "tools"), {"env": {"K": "v"}, "extra_args": secrets_rotation.APPLY_ARGS})]


def test_the_state_check_refuses_when_the_core_objects_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.deployment.local import platform

    provider = SimpleNamespace(config="config", tools="tools", env={})
    monkeypatch.setattr(secrets_rotation, "_local_provider", lambda _ctx: provider)
    monkeypatch.setattr(platform, "require_foundation_state", lambda _config: None)
    monkeypatch.setattr(platform, "platform_state_holds_core", lambda *a, **k: False)

    with pytest.raises(secrets_rotation.RotationError, match="platform state has drifted; run olf deploy first"):
        secrets_rotation.check_platform_state(context(tmp_path))


def test_a_missing_foundation_state_refuses_before_anything_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.deployment.local import platform

    config = SimpleNamespace(paths=SimpleNamespace(foundation_state_path=tmp_path / "missing.tfstate"))
    provider = SimpleNamespace(config=config, tools=None, env={})
    monkeypatch.setattr(secrets_rotation, "_local_provider", lambda _ctx: provider)
    monkeypatch.setattr(platform, "platform_state_holds_core", lambda *a, **k: True)
    kubectl = FakeKubectl()

    with pytest.raises(secrets_rotation.RotationError, match="foundation Terraform state is missing.*olf deploy first"):
        rotate(tmp_path, kubectl, platform_check=secrets_rotation.check_platform_state)

    assert kubectl.deletes() == []


@pytest.mark.parametrize("failing", ["prepare_charts", "require_no_stage_removal"])
def test_failing_setup_or_stage_removal_refuses_before_anything_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    from olf.deployment.errors import DeploymentPreconditionError
    from olf.deployment.local import platform

    config = SimpleNamespace(paths=SimpleNamespace(platform_terraform_dir=Path("p")), kube_context="k")
    tools = SimpleNamespace(terraform=SimpleNamespace(init=lambda *a, **k: None))
    provider = SimpleNamespace(config=config, tools=tools, env={})
    monkeypatch.setattr(secrets_rotation, "_local_provider", lambda _ctx: provider)
    monkeypatch.setattr(platform, "require_foundation_state", lambda _config: None)
    monkeypatch.setattr(platform, "platform_state_holds_core", lambda *a, **k: True)
    monkeypatch.setattr(platform, "prepare_charts", lambda *a, **k: None)
    monkeypatch.setattr(platform, "require_no_stage_removal", lambda *a, **k: None)

    def boom(*_a: Any, **_k: Any) -> None:
        raise DeploymentPreconditionError("boom")

    monkeypatch.setattr(platform, failing, boom)
    kubectl = FakeKubectl()

    with pytest.raises(secrets_rotation.RotationError, match="boom"):
        rotate(tmp_path, kubectl, platform_check=secrets_rotation.check_platform_state)

    assert kubectl.deletes() == []


def test_a_sibling_without_the_client_secret_key_refuses_before_anything_is_deleted(tmp_path: Path) -> None:
    kubectl = FakeKubectl(without_key=("dex-client-openmetadata",))

    with pytest.raises(secrets_rotation.RotationError, match="dex-client-openmetadata.*lacks the key.*olf deploy"):
        rotate(tmp_path, kubectl)

    assert kubectl.deletes() == []


def test_apply_targets_refuses_a_drifted_state_without_resetting_or_applying(monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.deployment.errors import DeploymentPreconditionError
    from olf.deployment.local import platform

    monkeypatch.setattr(platform.kube_ops, "state_has_resource", lambda *a, **k: False)
    applied: list[Any] = []
    tools = SimpleNamespace(terraform=SimpleNamespace(apply=lambda *a, **k: applied.append(k)))
    config = SimpleNamespace(paths=SimpleNamespace(platform_terraform_dir=Path("platform")))

    with pytest.raises(DeploymentPreconditionError, match="drifted; run olf deploy first"):
        platform.apply_targets(config, tools, env={}, extra_args=["-target=x"])  # type: ignore[arg-type]

    assert applied == []


def test_apply_targets_retries_a_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.deployment.errors import CommandExecutionError
    from olf.deployment.local import platform
    from olf.deployment.retry import RetryPolicy

    monkeypatch.setattr(platform, "platform_state_holds_core", lambda *a, **k: True)
    monkeypatch.setattr(platform, "platform_var_files", lambda *a, **k: [])
    monkeypatch.setattr(platform, "platform_apply_variables", lambda *a, **k: {})
    attempts: list[int] = []

    def flaky(*_: Any, **__: Any) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise CommandExecutionError(["terraform", "apply"], 1)

    tools = SimpleNamespace(terraform=SimpleNamespace(apply=flaky))
    config = SimpleNamespace(
        paths=SimpleNamespace(platform_terraform_dir=Path("platform")),
        terraform=SimpleNamespace(apply_retry=RetryPolicy(max_attempts=2, delay_seconds=0)),
    )

    platform.apply_targets(config, tools, env={}, extra_args=["-target=x"])  # type: ignore[arg-type]

    assert len(attempts) == 2


def test_a_missing_target_is_regenerated_without_a_delete(tmp_path: Path) -> None:
    kubectl = FakeKubectl({name: f"uid-{name}/1" for name in CLIENTS if name != "dex-client-perimeter"})
    applied: list[Any] = []

    rotation = rotate(
        tmp_path, kubectl, platform_apply=apply_that_regenerates(kubectl, "dex-client-perimeter", applied)
    )

    assert kubectl.deletes() == []
    assert len(applied) == 1
    assert rotation.was_missing
    assert ("restart", "deployment/dex", None) in kubectl.calls


def test_the_drill_skips_without_a_resolved_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.e2e import _rotation

    contracts = {"shared": {"access": {"perimeter": {"enabled": True}}, "identity": {"adapter": "dex"}}}
    monkeypatch.setattr(_rotation, "load_provider_contracts_or_raise", lambda _cfg: contracts)
    called: list[str] = []
    monkeypatch.setattr(_rotation.secrets_rotation, "rotate_client", lambda *a, **k: called.append("rotated"))

    _rotation.check_secret_rotation(SimpleNamespace(deployment=None))  # type: ignore[arg-type]

    assert called == []
