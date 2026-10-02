from __future__ import annotations

import contextlib
import socket
import tempfile
from pathlib import Path

from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.context import DeploymentContext
from olf.deployment.engine import DeploymentPhase, Toolkit
from olf.deployment.local.config import LocalDeploymentConfig
from olf.deployment.local.provider import LocalProvider
from olf.tooling.aws import AwsCli
from olf.tooling.azure import AzureCli
from olf.tooling.docker import Docker
from olf.tooling.helm import Helm
from olf.tooling.kind import Kind
from olf.tooling.kubectl import Kubectl
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver
from olf.tooling.terraform import Terraform

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm")


def _toolkit() -> Toolkit:
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0))
    resolver = PathExecutableResolver(overrides={tool: Path(tool) for tool in _TOOLS})
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
        aws=AwsCli(runner, resolver),
        azure=AzureCli(runner, resolver),
    )


def _config(tmp_path: Path) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path)
    return LocalDeploymentConfig.from_environment({}, context=context)


def test_env_excludes_ambient_variables_not_scoped_by_the_deployment(tmp_path: Path) -> None:
    ambient = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "postgres://prod-user:s3cr3t@db.internal/prod",
        "SENTRY_DSN": "https://public@sentry.example/1",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
    }

    provider = LocalProvider.create(_config(tmp_path), toolkit=_toolkit(), environ=ambient)

    env = provider.env

    assert "DATABASE_URL" not in env
    assert "SENTRY_DSN" not in env
    assert "PATH" not in env


def test_env_still_carries_every_deployment_scoped_key(tmp_path: Path) -> None:
    # DOCKER_HOST left unset so the ambient-context resolution path (covered
    # separately below) doesn't need scripting here.
    provider = LocalProvider.create(_config(tmp_path), toolkit=_toolkit(), environ={})

    env = provider.env

    assert env["KUBECONFIG"] == str(provider.context.paths.kubeconfig_path)
    assert env["KUBE_CONTEXT"] == provider.context.kube_context
    assert env["DOCKER_CONFIG"] == str(provider.context.paths.docker_config_dir)
    assert env["HELM_REPOSITORY_CONFIG"] == str(provider.context.paths.helm_repository_config)
    assert env["HELM_REPOSITORY_CACHE"] == str(provider.context.paths.helm_repository_cache)
    assert env["SUPERSET_REPORT_WORK_DIR"] == str(provider.context.paths.superset_report_work_dir)
    assert env["OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX"] == str(provider.context.paths.port_forward_log_prefix)


def test_env_resolves_docker_host_from_ambient_context_when_unset(tmp_path: Path) -> None:
    class _ScriptedRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            stdout = "colima\n" if argv[-2:] == ["context", "show"] else "unix:///colima/docker.sock\n"
            return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

    runner = _ScriptedRunner()
    resolver = PathExecutableResolver(overrides={tool: Path(tool) for tool in _TOOLS})
    toolkit = Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
        aws=AwsCli(runner, resolver),
        azure=AzureCli(runner, resolver),
    )

    provider = LocalProvider.create(_config(tmp_path), toolkit=toolkit, environ={})

    assert provider.env["DOCKER_HOST"] == "unix:///colima/docker.sock"


def test_env_falls_back_to_colima_socket_when_ambient_context_resolution_is_dead(tmp_path: Path) -> None:
    """The endpoint `docker context show`/`inspect` returns can point at a
    socket file left behind by a daemon that exited without unlinking it -
    #156's stale Docker Desktop socket. `LocalProvider.env` must not accept
    it just because the inode exists; it must fall back to a live Colima
    socket."""
    with tempfile.TemporaryDirectory(dir="/tmp") as home:
        stale_socket = Path(home) / "stale" / "docker.sock"
        stale_socket.parent.mkdir(parents=True, exist_ok=True)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(stale_socket))
        dead.close()  # leaves the socket-type inode behind with nothing listening

        default_socket = Path(home) / ".colima" / "default" / "docker.sock"
        default_socket.parent.mkdir(parents=True, exist_ok=True)
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(str(default_socket))
        live.listen(1)

        class _ScriptedRunner(RecordingRunner):
            def run(self, command, **kwargs):  # type: ignore[override]
                argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                stdout = "colima\n" if argv[-2:] == ["context", "show"] else f"unix://{stale_socket}\n"
                return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

        with contextlib.closing(live):
            runner = _ScriptedRunner()
            resolver = PathExecutableResolver(overrides={tool: Path(tool) for tool in _TOOLS})
            toolkit = Toolkit(
                runner=runner,
                resolver=resolver,
                terraform=Terraform(runner, resolver),
                helm=Helm(runner, resolver),
                kubectl=Kubectl(runner, resolver),
                docker=Docker(runner, resolver),
                kind=Kind(runner, resolver),
                aws=AwsCli(runner, resolver),
                azure=AzureCli(runner, resolver),
            )

            provider = LocalProvider.create(_config(tmp_path), toolkit=toolkit, environ={"HOME": home})

            assert provider.env["DOCKER_HOST"] == f"unix://{default_socket}"


def test_foundation_doctor_does_not_require_helm(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    provider = LocalProvider.create(_config(tmp_path), toolkit=_toolkit(), environ={})
    required: list[str] = []
    health_calls: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.local.provider.base_report",
        lambda **kwargs: required.extend(kwargs["required_tools"]) or [],
    )
    monkeypatch.setattr(
        "olf.deployment.local.provider.docker_health",
        lambda *args, **kwargs: health_calls.append("docker") or None,
    )

    provider.doctor(DeploymentPhase.FOUNDATION)

    assert required == ["terraform", "kubectl", "docker", "kind"]
    assert health_calls == ["docker"]


def test_artifacts_doctor_does_not_require_helm(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    provider = LocalProvider.create(_config(tmp_path), toolkit=_toolkit(), environ={})
    required: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.local.provider.base_report",
        lambda **kwargs: required.extend(kwargs["required_tools"]) or [],
    )
    monkeypatch.setattr("olf.deployment.local.provider.docker_health", lambda *args, **kwargs: None)
    monkeypatch.setattr("olf.contracts.load_provider_contracts", lambda *_args, **_kwargs: None)

    provider.doctor(DeploymentPhase.ARTIFACTS)

    assert required == ["terraform", "kubectl", "docker", "kind"]


def test_artifacts_doctor_requires_platform_provider_contracts(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    provider = LocalProvider.create(_config(tmp_path), toolkit=_toolkit(), environ={})
    monkeypatch.setattr("olf.deployment.local.provider.docker_health", lambda *args, **kwargs: None)
    monkeypatch.setattr("olf.contracts.load_provider_contracts", lambda *_args, **_kwargs: None)

    report = provider.doctor(DeploymentPhase.ARTIFACTS)

    contracts_item = next(
        item for item in report.items if item is not None and item.name == "local platform provider contracts"
    )
    assert contracts_item.ok is False


def test_artifacts_doctor_uses_the_configured_contract_root(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    contract_root = tmp_path / "custom-platform"
    observed: list[str] = []
    provider = LocalProvider.create(
        _config(tmp_path), toolkit=_toolkit(), environ={"OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR": str(contract_root)}
    )
    monkeypatch.setattr("olf.deployment.local.provider.docker_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "olf.contracts.load_provider_contracts",
        lambda path, **_kwargs: observed.append(path) or {"schema_version": "2.0.0"},
    )

    report = provider.doctor(DeploymentPhase.ARTIFACTS)

    assert observed == [str(contract_root.resolve())]
    contracts_item = next(
        item for item in report.items if item is not None and item.name == "local platform provider contracts"
    )
    assert contracts_item.ok is True


def test_platform_plan_prepares_cached_chart(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    config = _config(tmp_path)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    provider = LocalProvider.create(config, toolkit=_toolkit(), environ={})
    calls: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.local.platform.prepare_charts", lambda *args, **kwargs: calls.append("charts")
    )

    provider.plan(DeploymentPhase.PLATFORM)

    assert calls == ["charts"]


def test_build_project_image_pushes_so_a_revision_can_name_a_digest(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Local has no registry of its own; a revision still needs a pullable digest.

    A kind-only image has just a local config Id, which `olf project build`
    rejects, so the default has to publish to whatever
    `PROJECT_CODE_IMAGE_REPOSITORY` names.
    """
    from olf.deployment.local import images
    from olf.deployment.local import provider as provider_module

    monkeypatch.setattr(images, "build_project_code_image", lambda *a, **k: "ghcr.io/acme/project-code:dev")
    monkeypatch.setattr(images, "load_image_into_kind", lambda *a, **k: None)
    monkeypatch.setattr(provider_module.log, "step", lambda _message: None)
    pushed: list[str] = []

    provider = LocalProvider.create(_config(tmp_path), toolkit=_toolkit(), environ={})
    monkeypatch.setattr(provider.tools.docker, "push", lambda image, **k: pushed.append(image))

    assert provider.build_project_image() == "ghcr.io/acme/project-code:dev"
    assert pushed == ["ghcr.io/acme/project-code:dev"]

    pushed.clear()
    provider.build_project_image(push=False)

    assert pushed == []
