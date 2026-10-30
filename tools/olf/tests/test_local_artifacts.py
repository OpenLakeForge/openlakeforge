from __future__ import annotations

import os
from pathlib import Path

import pytest

from olf import contracts as contracts_module
from olf.deployment.context import DeploymentContext, Profile
from olf.deployment.local import artifacts
from olf.deployment.local.config import LocalDeploymentConfig
from olf.tooling.toolkit import Toolkit


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path, profile=profile)
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit() -> Toolkit:
    return Toolkit.default(
        overrides={t: Path(t) for t in ("terraform", "docker", "kind", "kubectl", "helm")}
    )


def test_applied_contract_environment_applies_and_restores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr("olf.contract_state.load_provider_contracts", lambda terraform_dir: None)
    monkeypatch.setattr(
        contracts_module,
        "build_contract_env",
        lambda base, contracts_value, *, repo_root, **_: (
            {"OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev"},
            ["SOME_STALE_VAR"],
        ),
    )
    monkeypatch.setenv("SOME_STALE_VAR", "old-value")
    monkeypatch.delenv("OPENLAKEFORGE_CATALOG_NAME", raising=False)

    with artifacts.applied_contract_environment(config):
        assert os.environ["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"
        assert "SOME_STALE_VAR" not in os.environ
        assert os.environ["NAMESPACE"] == "olf-dev"

    assert "OPENLAKEFORGE_CATALOG_NAME" not in os.environ
    assert os.environ["SOME_STALE_VAR"] == "old-value"


def test_applied_contract_environment_honors_contract_root_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    override = tmp_path / "custom-contracts"
    captured: dict[str, Path] = {}
    monkeypatch.setenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(override))
    monkeypatch.setattr(
        artifacts.contract_env,
        "applied_contract_environment",
        lambda **kwargs: captured.update(kwargs) or _empty_contract_environment(),
    )

    with artifacts.applied_contract_environment(config):
        pass

    assert captured["contract_terraform_dir"] == override


def test_artifacts_deploy_preserves_step_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = _toolkit()
    calls: list[str] = []

    monkeypatch.setattr(
        artifacts,
        "applied_contract_environment",
        _fake_contextmanager(lambda: calls.append("contract_env_entered")),
    )
    monkeypatch.setattr(artifacts, "sync_catalog_namespaces", lambda: calls.append("sync_namespaces"))
    monkeypatch.setattr(
        artifacts.images,
        "prepare_project_code_image",
        lambda cfg, tls, *, env: calls.append("prepare_project_code_image"),
    )
    monkeypatch.setattr(
        artifacts.k8s, "set_project_code_image", lambda image, namespace: calls.append("set_project_code_image")
    )
    monkeypatch.setattr(
        artifacts, "deploy_optional_layer_artifacts", lambda environ: calls.append("deploy_optional_layers")
    )

    artifacts.artifacts_deploy(config, tools, env={})

    assert calls == [
        "contract_env_entered",
        "sync_namespaces",
        "prepare_project_code_image",
        "set_project_code_image",
        "deploy_optional_layers",
    ]


def _fake_contextmanager(on_enter):  # noqa: ANN001, ANN202
    from contextlib import contextmanager

    @contextmanager
    def _cm(config, *, environ=None):  # noqa: ANN001, ARG001
        on_enter()
        yield dict(os.environ)

    return _cm


def _empty_contract_environment():  # noqa: ANN202
    from contextlib import nullcontext

    return nullcontext({})
