from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from olf.commands import runtime


def test_standalone_contract_context_honors_custom_contract_root(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    default = tmp_path / "default-contracts"
    override = tmp_path / "custom-contracts"
    monkeypatch.setenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(override))

    assert runtime._contract_terraform_dir(default) == override


def test_standalone_local_contract_context_honors_custom_contract_root(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    from olf.deployment.context import DeploymentContext
    from olf.deployment.engine import Toolkit
    from olf.deployment.local.config import LocalDeploymentConfig
    from olf.deployment.local.provider import LocalProvider

    context = DeploymentContext.local(repo_root=tmp_path)
    config = LocalDeploymentConfig.from_environment({}, context=context)
    provider = LocalProvider.create(config, toolkit=Toolkit.default())
    override = tmp_path / "custom-contracts"
    captured: dict[str, Path] = {}
    monkeypatch.setenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(override))
    monkeypatch.setattr("olf.commands.deployment._build_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(
        "olf.commands.deployment._build_engine", lambda *args, **kwargs: SimpleNamespace(provider=provider)
    )
    monkeypatch.setattr(
        "olf.deployment.local.artifacts.applied_contract_environment",
        lambda config, *, contract_terraform_dir, environ=None: captured.update(
            contract_terraform_dir=contract_terraform_dir
        )
        or nullcontext(),
    )

    with runtime.provider_contract_environment(
        provider="local", profile="full", namespace="", cluster_name="", kubeconfig_path=""
    ):
        pass

    assert captured["contract_terraform_dir"] == override


def test_provider_contract_environment_threads_project_root_to_build_context(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """A custom --project-root must reach `_build_context`, not just the bundled-demo default.

    Standalone commands (`olf report import`, `olf dbt parse`, etc.)
    call this helper outside the full `olf deploy` flow - without threading
    `project_root` through, they always resolve the bundled demo project
    regardless of what the user selected.
    """
    from olf.deployment.context import DeploymentContext
    from olf.deployment.engine import Toolkit
    from olf.deployment.local.config import LocalDeploymentConfig
    from olf.deployment.local.provider import LocalProvider

    context = DeploymentContext.local(repo_root=tmp_path)
    config = LocalDeploymentConfig.from_environment({}, context=context)
    provider = LocalProvider.create(config, toolkit=Toolkit.default())
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "olf.commands.deployment._build_context",
        lambda *args, **kwargs: captured.update(kwargs) or context,
    )
    monkeypatch.setattr(
        "olf.commands.deployment._build_engine", lambda *args, **kwargs: SimpleNamespace(provider=provider)
    )
    monkeypatch.setattr(
        "olf.deployment.local.artifacts.applied_contract_environment",
        lambda config, *, contract_terraform_dir, environ=None: nullcontext(),
    )

    with runtime.provider_contract_environment(
        provider="local",
        profile="full",
        namespace="",
        cluster_name="",
        kubeconfig_path="",
        project_root=str(tmp_path / "my-project"),
    ):
        pass

    assert captured["project_root"] == str(tmp_path / "my-project")
