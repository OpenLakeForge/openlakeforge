from __future__ import annotations

import os
from pathlib import Path

import pytest

from olf.infra import contracts as contracts_module
from olf.infra.deployment import contract_env


def _kwargs(tmp_path: Path, **overrides: object) -> dict[str, object]:
    base = {
        "contract_terraform_dir": tmp_path / "infra/terraform/environments/aws-poc",
        "repo_root": tmp_path,
        "namespace": "lakehouse",
        "kube_context": "eks-openlakeforge-poc",
        "kubeconfig_path": tmp_path / ".tmp/kubeconfigs/aws.yaml",
        "port_forward_log_prefix": Path("/tmp/openlakeforge-aws"),
    }
    base.update(overrides)
    return base


def test_yields_contract_exports_without_touching_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda terraform_dir: None)
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
    before = dict(os.environ)

    with contract_env.applied_contract_environment(**_kwargs(tmp_path)) as env:
        assert dict(os.environ) == before
        assert env["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"
        assert "SOME_STALE_VAR" not in env
        assert env["NAMESPACE"] == "lakehouse"
        assert env["KUBE_CONTEXT"] == "eks-openlakeforge-poc"

    assert dict(os.environ) == before


def test_extra_variables_override_contract_exports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda terraform_dir: None)
    monkeypatch.setattr(
        contracts_module,
        "build_contract_env",
        lambda base, contracts_value, *, repo_root, **_: ({"NAMESPACE": "from-contract"}, []),
    )

    with contract_env.applied_contract_environment(**_kwargs(tmp_path, namespace="lakehouse-slim")) as env:
        assert env["NAMESPACE"] == "lakehouse-slim"


def test_restores_environment_even_when_the_block_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda terraform_dir: None)
    monkeypatch.setattr(
        contracts_module,
        "build_contract_env",
        lambda base, contracts_value, *, repo_root, **_: ({"OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev"}, []),
    )
    monkeypatch.delenv("OPENLAKEFORGE_CATALOG_NAME", raising=False)

    with pytest.raises(RuntimeError):
        with contract_env.applied_contract_environment(**_kwargs(tmp_path)):
            raise RuntimeError("boom")

    assert "OPENLAKEFORGE_CATALOG_NAME" not in os.environ


def test_uses_the_scoped_environment_for_contract_terraform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: dict[str, str] = {}

    def _load(terraform_dir: str, *, environ: dict[str, str]) -> None:
        observed.update(environ)
        assert terraform_dir == str(tmp_path / "infra/terraform/environments/aws-poc")
        return None

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", _load)
    monkeypatch.setattr(contracts_module, "build_contract_env", lambda *args, **kwargs: ({}, []))

    with contract_env.applied_contract_environment(
        **_kwargs(tmp_path),
        environ={
            "OLF_DISTRIBUTION_ROOT": str(tmp_path / "payload"),
            "OPENLAKEFORGE_TERRAFORM_STATE_ROOT": str(tmp_path / "state/aws"),
            "OPENLAKEFORGE_TERRAFORM_DATA_ROOT": str(tmp_path / "work/aws/terraform-data"),
        },
    ) as env:
        assert env["OLF_DISTRIBUTION_ROOT"] == str(tmp_path / "payload")

    assert observed["OLF_DISTRIBUTION_ROOT"] == str(tmp_path / "payload")
    assert observed["OPENLAKEFORGE_TERRAFORM_STATE_ROOT"] == str(tmp_path / "state/aws")


def test_selected_stage_survives_into_the_contract_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`openlakeforge_runtime.product_dagster.build_stage_schedules` reads OPENLAKEFORGE_STAGE in the code-server
    pod, and the only thing that puts it there is this environment: activation forwards every
    `OPENLAKEFORGE_*` key it yields onto the container. A contract that dropped the key would
    silently give PROD the unrecognised-stage behaviour, which is no schedules at all."""
    from olf.common.deployment.context import DeploymentContext

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda terraform_dir, *, environ: None)
    monkeypatch.setattr(contracts_module, "build_contract_env", lambda *args, **kwargs: ({}, []))
    context = DeploymentContext.local(repo_root=tmp_path, stage="dev")

    with contract_env.applied_contract_environment(**_kwargs(tmp_path), environ=context.command_env()) as env:
        assert env["OPENLAKEFORGE_STAGE"] == "dev"


def test_yields_read_only_contract_environment_with_stage_coordinates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda terraform_dir: None)
    monkeypatch.setattr(
        contracts_module,
        "build_contract_env",
        lambda base, contracts_value, *, repo_root, **_: ({"OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev"}, ["GONE"]),
    )
    monkeypatch.setenv("GONE", "stale")

    with contract_env.applied_contract_environment(**_kwargs(tmp_path, stage="dev")) as env:
        assert env.kube_context == "eks-openlakeforge-poc"
        assert env.namespace == "lakehouse"
        assert env.kubeconfig_path == tmp_path / ".tmp/kubeconfigs/aws.yaml"
        assert env.stage == "dev"
        assert env.unsets == ("GONE",)
        assert env.environ["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"
        assert env["NAMESPACE"] == "lakehouse"
        assert "GONE" not in env
        assert "GONE" not in env.environ
        assert dict(env) == dict(env.environ)
        with pytest.raises(TypeError):
            env.environ["X"] = "y"  # type: ignore[index]  # read-only mapping is the behaviour under test
        with pytest.raises(TypeError):
            env["X"] = "y"  # type: ignore[index]  # Mapping has no __setitem__


def _docker_config_seen_by_pull(tmp_path: Path, provider_env: dict[str, str]) -> str | None:
    """`DOCKER_CONFIG` as a real docker subprocess receives it from a pull issued the way activation does."""
    import json

    from _tooling_support import write_fake_executable

    from olf.common.tooling.docker import Docker, ambient_registry_env
    from olf.common.tooling.process import ProcessRunner
    from olf.common.tooling.resolver import PathExecutableResolver

    fake = write_fake_executable(tmp_path / "docker", env_keys=["DOCKER_CONFIG"])
    docker = Docker(ProcessRunner(), PathExecutableResolver(overrides={"docker": fake}))
    with contract_env.applied_contract_environment(**_kwargs(tmp_path), environ=provider_env):
        result = docker.pull("ghcr.io/acme/code@sha256:abc", env=ambient_registry_env(provider_env))
    seen: str | None = json.loads(result.stdout)["env"]["DOCKER_CONFIG"]
    return seen


@pytest.fixture
def no_contract_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda terraform_dir, **_: None)
    monkeypatch.setattr(contracts_module, "build_contract_env", lambda base, value, **_: ({}, []))


@pytest.mark.usefixtures("no_contract_exports")
def test_pull_in_the_contract_environment_runs_without_docker_config_when_none_is_ambient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)

    assert _docker_config_seen_by_pull(tmp_path, {"DOCKER_CONFIG": str(tmp_path / "olf-scoped")}) is None


@pytest.mark.usefixtures("no_contract_exports")
def test_pull_in_the_contract_environment_uses_the_developers_docker_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")

    assert _docker_config_seen_by_pull(tmp_path, {"DOCKER_CONFIG": str(tmp_path / "olf-scoped")}) == "/home/dev/.docker"
    assert os.environ["DOCKER_CONFIG"] == "/home/dev/.docker"
