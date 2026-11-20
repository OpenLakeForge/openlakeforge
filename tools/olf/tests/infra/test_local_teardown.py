from __future__ import annotations

from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.common.deployment.context import DeploymentContext, Profile
from olf.common.deployment.errors import DeploymentPreconditionError, ExecutableNotFoundError
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.local import teardown

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm")


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path, profile=profile)
    config = LocalDeploymentConfig.from_environment({}, context=context)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    return config


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.common.tooling.docker import Docker
    from olf.common.tooling.helm import Helm
    from olf.common.tooling.kind import Kind
    from olf.common.tooling.kubectl import Kubectl
    from olf.common.tooling.terraform import Terraform

    resolver = PathExecutableResolver(overrides={t: Path(t) for t in _TOOLS})
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
    )


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


def _fail() -> CommandResult:
    return CommandResult(argv=(), returncode=1, stdout="", stderr="", duration_seconds=0.0)


def test_platform_down_raises_when_context_unreachable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = _toolkit(_UnreachableRunner(live_port=43175))

    with pytest.raises(DeploymentPreconditionError, match="not reachable"):
        teardown.platform_down(config, tools, env={})


def _kubeconfig(port: int, *, cluster: str = "openlakeforge-local") -> str:
    name = f"kind-{cluster}"
    return (
        "apiVersion: v1\nkind: Config\n"
        f"clusters:\n- name: {name}\n  cluster:\n    server: https://127.0.0.1:{port}\n"
        f"contexts:\n- name: {name}\n  context:\n    cluster: {name}\n    user: {name}\n"
        f"current-context: {name}\n"
    )


class _UnreachableRunner(RecordingRunner):
    """kubectl cannot reach the context; kind reports the cluster on `live_port`, or none."""

    def __init__(self, live_port: int | None) -> None:
        super().__init__()
        self._live_port = live_port

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if argv[:3] == ["kind", "get", "kubeconfig"]:
            if self._live_port is None:
                return _fail()
            return _ok(_kubeconfig(self._live_port))
        return _fail()


def _unreachable_message(config: LocalDeploymentConfig, runner: RecordingRunner) -> str:
    with pytest.raises(DeploymentPreconditionError) as raised:
        teardown.platform_down(config, _toolkit(runner), env={})
    return str(raised.value)


def test_unreachable_context_names_the_kubeconfig_and_state_root_it_read(tmp_path: Path) -> None:
    config = _config(tmp_path)

    message = _unreachable_message(config, _UnreachableRunner(live_port=43175))

    assert str(config.paths.kubeconfig_path) in message
    assert str(tmp_path / ".tmp") in message
    assert str(config.paths.foundation_state_path) in message


def test_source_teardown_points_at_the_installed_state_that_owns_the_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported case: deployed with the installed olf, torn down from the
    source tree, whose `.tmp` kubeconfig was left by an earlier deployment."""
    home = tmp_path / "olf-home"
    monkeypatch.setenv("OLF_HOME", str(home))
    installed_kubeconfig = home / "state" / "local" / "kubeconfig.yaml"
    installed_kubeconfig.parent.mkdir(parents=True)
    installed_kubeconfig.write_text(_kubeconfig(43175))
    config = _config(tmp_path / "repo")
    config.paths.kubeconfig_path.parent.mkdir(parents=True)
    config.paths.kubeconfig_path.write_text(_kubeconfig(34243))

    message = _unreachable_message(config, _UnreachableRunner(live_port=43175))

    assert "running at https://127.0.0.1:43175" in message
    assert "points at https://127.0.0.1:34243" in message
    assert "another deployment of 'openlakeforge-local'" in message
    assert f"installed olf kubeconfig {installed_kubeconfig} matches the running cluster" in message


def test_installed_teardown_points_at_the_source_tree_state_that_owns_the_cluster(tmp_path: Path) -> None:
    project = tmp_path / "project"
    context = DeploymentContext.local(repo_root=project, state_root=tmp_path / "olf-home" / "state")
    config = LocalDeploymentConfig.from_environment({}, context=context)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    source_kubeconfig = project / ".tmp" / "kubeconfigs" / "local.yaml"
    source_kubeconfig.parent.mkdir(parents=True)
    source_kubeconfig.write_text(_kubeconfig(34243))

    message = _unreachable_message(config, _UnreachableRunner(live_port=34243))

    assert f"installed olf state root {tmp_path / 'olf-home' / 'state' / 'local'}" in message
    assert "has no server for that context" in message
    assert f"source-tree olf kubeconfig {source_kubeconfig} matches the running cluster" in message
    assert "uv run --project tools/olf --locked olf infra destroy" in message


def test_unreachable_context_does_not_point_at_another_root_that_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "olf-home"
    monkeypatch.setenv("OLF_HOME", str(home))
    installed_kubeconfig = home / "state" / "local" / "kubeconfig.yaml"
    installed_kubeconfig.parent.mkdir(parents=True)
    installed_kubeconfig.write_text(_kubeconfig(40000))
    config = _config(tmp_path / "repo")

    message = _unreachable_message(config, _UnreachableRunner(live_port=43175))

    assert "another deployment" in message
    assert str(installed_kubeconfig) not in message


def test_platform_down_raises_when_foundation_state_missing(tmp_path: Path) -> None:
    context = DeploymentContext.local(repo_root=tmp_path)
    config = LocalDeploymentConfig.from_environment({}, context=context)
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)

    with pytest.raises(DeploymentPreconditionError, match="foundation Terraform state is missing"):
        teardown.platform_down(config, tools, env={})


class _TeardownScriptedRunner(RecordingRunner):
    def __init__(self, *, helm_releases_present: set[str] | None = None) -> None:
        super().__init__()
        self._present = helm_releases_present or set()

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if argv[0] == "helm" and "status" in argv:
            release = argv[-3]
            return _ok() if release in self._present else _fail()
        return _ok()


def test_platform_down_orders_superset_job_delete_before_destroy_and_namespace_delete_last(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _TeardownScriptedRunner()
    tools = _toolkit(runner)

    teardown.platform_down(config, tools, env={})

    kinds = []
    for call in runner.calls:
        if "superset-init-db" in call.argv:
            kinds.append("superset-job-delete")
        elif call.argv[0] == "terraform" and "destroy" in call.argv:
            kinds.append("terraform-destroy")
        elif "namespace" in call.argv and "delete" in call.argv:
            kinds.append("namespace-delete")
    # One Superset job per enabled stage, then destroy, then every namespace
    # this deployment owns: the shared one plus one per stage.
    assert kinds == ["superset-job-delete", "terraform-destroy", "namespace-delete", "namespace-delete"]


def test_platform_down_destroy_uses_the_topology_var_subset(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _TeardownScriptedRunner()
    tools = _toolkit(runner)

    teardown.platform_down(config, tools, env={})

    destroy_call = next(c for c in runner.calls if c.argv[0] == "terraform" and "destroy" in c.argv)
    assert "-var=shared_namespace=olf-system" in destroy_call.argv
    assert not any("project_code_image" in arg for arg in destroy_call.argv)
    assert not any("trino_chart_package_path" in arg for arg in destroy_call.argv)


def test_cleanup_legacy_helm_releases_only_uninstalls_present_releases(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _TeardownScriptedRunner(helm_releases_present={"trino", "garage"})
    tools = _toolkit(runner)

    removed = teardown.cleanup_legacy_helm_releases(config, tools, env={})

    assert removed == ["trino", "garage"]
    uninstall_releases = [c.argv[c.argv.index("uninstall") + 1] for c in runner.calls if "uninstall" in c.argv]
    assert uninstall_releases == ["trino", "garage"]


def test_cleanup_legacy_helm_releases_skips_when_helm_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class MissingHelmRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            raise ExecutableNotFoundError("helm")

    resolver = PathExecutableResolver(overrides={"terraform": Path("terraform"), "kubectl": Path("kubectl")})
    from olf.common.tooling.docker import Docker
    from olf.common.tooling.helm import Helm
    from olf.common.tooling.kind import Kind
    from olf.common.tooling.kubectl import Kubectl
    from olf.common.tooling.terraform import Terraform

    runner = MissingHelmRunner()
    tools = Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
    )

    removed = teardown.cleanup_legacy_helm_releases(config, tools, env={})

    assert removed == []


def test_platform_down_deletes_every_namespace_the_deployment_owns(tmp_path: Path) -> None:
    """Teardown that only removed the selected stage's namespace would leave
    the shared services and every other stage behind."""
    config = _config(tmp_path)
    runner = _TeardownScriptedRunner()
    tools = _toolkit(runner)

    teardown.platform_down(config, tools, env={})

    deleted = [
        call.argv[call.argv.index("namespace") + 1]
        for call in runner.calls
        if "namespace" in call.argv and "delete" in call.argv
    ]
    assert deleted == ["olf-system", "olf-dev"]


def test_platform_down_restores_the_stock_corefile(tmp_path: Path) -> None:
    """The in-cluster resolution module owns the Corefile key and deletes it on
    destroy; the foundation outlives a platform-only teardown and needs DNS."""
    runner = _TeardownScriptedRunner()

    teardown.platform_down(_config(tmp_path), _toolkit(runner), env={})

    patches = [c.argv for c in runner.calls if "patch" in c.argv]
    assert len(patches) == 1 and "coredns" in patches[0] and "rewrite" not in patches[0][-1]


def test_platform_down_restores_the_corefile_when_destroy_fails_partway(tmp_path: Path) -> None:
    class _DestroyFails(_TeardownScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            result = super().run(command, **kwargs)
            if self.calls[-1].argv[0] == "terraform" and "destroy" in self.calls[-1].argv:
                raise RuntimeError("destroy failed")
            return result

    runner = _DestroyFails()

    with pytest.raises(RuntimeError, match="destroy failed"):
        teardown.platform_down(_config(tmp_path), _toolkit(runner), env={})

    assert any("patch" in c.argv and "coredns" in c.argv for c in runner.calls)


def test_platform_down_also_removes_a_stage_the_profile_no_longer_enables(tmp_path: Path) -> None:
    """Drift recovery tears down what is in the cluster, not only what the
    current topology names: a stage deployed and since disabled has no state
    left to destroy it, so it would survive a reset that reports success."""
    config = _config(tmp_path)

    class _Runner(_TeardownScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(part) for part in command]
            if "get" in argv and "namespace" in argv and "-l" in argv:
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                return CommandResult(
                    argv=(), returncode=0, stdout="olf-system\nolf-dev\nolf-prod\n", stderr="", duration_seconds=0.0
                )
            return super().run(command, **kwargs)

    runner = _Runner()
    teardown.platform_down(config, _toolkit(runner), env={})

    deleted = [
        call.argv[call.argv.index("namespace") + 1]
        for call in runner.calls
        if "namespace" in call.argv and "delete" in call.argv
    ]
    assert deleted == ["olf-system", "olf-dev", "olf-prod"]


def test_platform_down_fails_closed_when_namespace_discovery_errors(tmp_path: Path) -> None:
    """A label query that matches nothing succeeds and returns no names, so a
    failure is a real one. Reading it as "none found" would quietly reduce
    teardown to the current topology and then report success -- the outcome
    the discovery exists to prevent."""
    config = _config(tmp_path)

    class _Runner(_TeardownScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(part) for part in command]
            if "get" in argv and "namespace" in argv and "-l" in argv:
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                return CommandResult(
                    argv=(),
                    returncode=1,
                    stdout="",
                    stderr='namespaces is forbidden: User cannot list resource "namespaces"',
                    duration_seconds=0.0,
                )
            return super().run(command, **kwargs)

    runner = _Runner()

    with pytest.raises(DeploymentPreconditionError, match="cannot list resource"):
        teardown.platform_down(config, _toolkit(runner), env={})


def test_namespace_discovery_asks_kubectl_for_a_newline_separated_list(tmp_path: Path) -> None:
    """kubectl's jsonpath takes the escape as two characters. A real newline
    in the expression makes it an unterminated quoted string, and every caller
    of this then fails closed on a query that never ran."""
    config = _config(tmp_path)
    runner = _TeardownScriptedRunner()

    teardown.managed_namespaces(config, _toolkit(runner), env={})

    call = next(c for c in runner.calls if "namespace" in c.argv and "-l" in c.argv)
    expression = call.argv[call.argv.index("-o") + 1]
    assert expression == 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'
    assert "\n" not in expression


def test_platform_down_clears_state_of_a_cluster_deleted_outside_olf(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.paths.platform_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.platform_state_path.write_text("{}")
    runner = _UnreachableRunner(live_port=None)

    teardown.platform_down(config, _toolkit(runner), env={})

    assert not config.paths.foundation_state_path.exists()
    assert not config.paths.platform_state_path.exists()
    assert not any(call.argv[0] == "terraform" for call in runner.calls)


def test_platform_down_keeps_state_when_the_cluster_runs_but_the_kubeconfig_is_another_deployments(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with pytest.raises(DeploymentPreconditionError, match="belongs to another deployment"):
        teardown.platform_down(config, _toolkit(_UnreachableRunner(live_port=43175)), env={})

    assert config.paths.foundation_state_path.exists()
