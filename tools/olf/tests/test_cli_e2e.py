from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from olf.cli import app

runner = CliRunner()


_EXTERNAL_IDENTITY_YAML = (
    "  identity:\n"
    "    issuer: external\n"
    "    issuer_url: https://login.example.com/\n"
    "    role_claim: groups\n"
    "    role_mapping:\n"
    "      platform-admin: [olf-admins]\n"
)


@pytest.fixture(autouse=True)
def contract_env_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """`applied_contract_environment` shells out to `terraform output`; every test here mocks
    the contracts module so no real subprocess runs.

    The recorded `topology` is what a v3 contract is parsed against, so it is
    the only observable that says which Deployment Profile this command
    resolved.
    """
    from olf import contracts as contracts_module

    calls: list[dict] = []

    def _build_contract_env(base, contracts_value, *, repo_root, topology=None, stage=None, **_):  # noqa: ANN001, ANN202, ARG001
        calls.append({"repo_root": repo_root, "topology": topology, "stage": stage})
        return ({}, [])

    monkeypatch.setattr(contracts_module, "load_provider_contracts", lambda terraform_dir, *, environ=None: None)
    monkeypatch.setattr(contracts_module, "build_contract_env", _build_contract_env)
    return calls


def _write_profile(
    path: Path,
    *,
    name: str,
    preset: str,
    provider: str = "local",
    region: str = "",
    stages: tuple[str, ...] = ("dev",),
) -> None:
    provider_block = f"    type: {provider}\n" + (f"    region: {region}\n" if region else "")
    stage_block = "".join(f"    {stage}:\n      enabled: true\n" for stage in stages)
    path.write_text(
        "apiVersion: openlakeforge.io/v1alpha1\n"
        "kind: DeploymentProfile\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  provider:\n"
        f"{provider_block}"
        f"  preset: {preset}\n"
        "  stages:\n"
        f"{stage_block}" + ("" if provider == "local" else _EXTERNAL_IDENTITY_YAML),
        encoding="utf-8",
    )


def test_e2e_run_rejects_unknown_env() -> None:
    result = runner.invoke(app, ["e2e", "run", "--env", "bogus"])

    assert result.exit_code == 1
    assert "unknown --env" in result.output


def test_e2e_run_rejects_unknown_suite() -> None:
    result = runner.invoke(app, ["e2e", "run", "--env", "local", "--suite", "bogus"])

    assert result.exit_code == 1
    assert "unknown --suite" in result.output


def test_e2e_run_is_self_sufficient_no_shell_wrapper_needed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    def _fake_run(  # noqa: ANN001
        env, *, suite, checks, namespace, shared_namespace, kube_context, repo_root, distribution_root, deployment
    ):
        calls.append(
            {"env": env, "suite": suite, "namespace": namespace, "kube_context": kube_context, "repo_root": repo_root}
        )

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr("olf.e2e.run", _fake_run)

    result = runner.invoke(app, ["e2e", "run", "--env", "local"])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "env": "local",
            "suite": None,
            "namespace": "olf-dev",
            "kube_context": "kind-openlakeforge-local",
            "repo_root": tmp_path,
        }
    ]


def test_e2e_run_falls_back_to_provider_cluster_name_when_kube_context_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `aws-e2e`/`azure-e2e` Make targets never set `KUBE_CONTEXT` - only
    `AWS_CLUSTER_NAME`/`AZURE_CLUSTER_NAME`. `applied_contract_environment` always
    exports `KUBE_CONTEXT`, so it must resolve to the provider's cluster-name
    fallback rather than an empty string that would defeat `olf.e2e.run`'s own
    `os.environ.get("KUBE_CONTEXT", ...)` fallback.
    """
    calls: list[dict] = []

    def _fake_run(  # noqa: ANN001
        env, *, suite, checks, namespace, shared_namespace, kube_context, repo_root, distribution_root, deployment
    ):
        calls.append({"kube_context": kube_context})

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AWS_CLUSTER_NAME", "limited-eks-openlakeforge-poc")
    monkeypatch.delenv("KUBE_CONTEXT", raising=False)
    monkeypatch.setattr("olf.e2e.run", _fake_run)

    result = runner.invoke(app, ["e2e", "run", "--env", "aws"])

    assert result.exit_code == 0, result.output
    assert calls == [{"kube_context": "limited-eks-openlakeforge-poc"}]


def test_e2e_run_honors_provider_kubeconfig_path_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A direct `olf e2e run --env aws` invocation with only the documented
    `AWS_KUBECONFIG_PATH` override set (no bare `KUBECONFIG`) must still
    target that file - this command exports `KUBECONFIG` via
    `applied_contract_environment` before `_runner.configure_kubeconfig`
    ever runs, so it has to resolve the same provider-override precedence
    itself or it would shadow the override with the plain default path.
    """
    override = tmp_path / "custom/aws-kubeconfig.yaml"
    seen: dict = {}

    def _fake_run(  # noqa: ANN001, ARG001
        env, *, suite, checks, namespace, shared_namespace, kube_context, repo_root, distribution_root, deployment
    ):
        seen["kubeconfig"] = os.environ.get("KUBECONFIG")

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AWS_KUBECONFIG_PATH", str(override))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr("olf.e2e.run", _fake_run)

    result = runner.invoke(app, ["e2e", "run", "--env", "aws"])

    assert result.exit_code == 0, result.output
    assert seen["kubeconfig"] == str(override)


def test_e2e_run_resolves_installed_layout_via_deployment_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installed distribution's project root (the bundled demo, or
    `--project-root`) is not where its Terraform roots or kubeconfig live -
    both must come from the same `DeploymentContext` `olf deploy` builds
    (`distribution_root`, `context.paths.platform_terraform_dir`,
    `context.paths.kubeconfig_path`), not from the caller's current
    directory. This exercises `deployment_context` being called with
    `--project-root` and every downstream default following the returned
    context's paths instead of `config.repo_root()`."""
    from olf import contracts as contracts_module
    from olf.deployment.context import DeploymentContext

    project_root = tmp_path / "project"
    distribution_root = tmp_path / "distribution"
    context = DeploymentContext.local(
        repo_root=project_root,
        distribution_root=distribution_root,
        state_root=tmp_path / "state",
        work_root=tmp_path / "work",
        cache_root=tmp_path / "cache",
    )

    captured_context_call: dict = {}

    def _fake_deployment_context(env_arg, *, profile, namespace, cluster_name, project_root, stage):  # noqa: ANN001
        captured_context_call.update(
            env=env_arg, profile=profile, namespace=namespace, cluster_name=cluster_name, project_root=project_root
        )
        return context

    contract_dirs_seen: list[str] = []

    def _fake_load_contracts(terraform_dir, *, environ=None):  # noqa: ANN001, ARG001
        contract_dirs_seen.append(terraform_dir)
        return None

    monkeypatch.setattr("olf.commands.e2e.deployment_context", _fake_deployment_context)
    monkeypatch.setattr(contracts_module, "load_provider_contracts", _fake_load_contracts)

    calls: list[dict] = []

    def _fake_run(  # noqa: ANN001
        env, *, suite, checks, namespace, shared_namespace, kube_context, repo_root, distribution_root, deployment
    ):
        calls.append(
            {
                "repo_root": repo_root,
                "distribution_root": distribution_root,
                "kubeconfig": os.environ.get("KUBECONFIG"),
            }
        )

    monkeypatch.setattr("olf.e2e.run", _fake_run)

    result = runner.invoke(app, ["e2e", "run", "--env", "local", "--project-root", str(project_root)])

    assert result.exit_code == 0, result.output
    assert captured_context_call["project_root"] == str(project_root)
    assert contract_dirs_seen == [str(context.paths.platform_terraform_dir)]
    assert calls == [
        {
            "repo_root": context.paths.repo_root,
            "distribution_root": context.paths.distribution_root,
            "kubeconfig": str(context.paths.kubeconfig_path),
        }
    ]


def test_e2e_run_maps_e2e_error_to_exit_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from olf import e2e

    def _raise(*a, **k):  # noqa: ANN002, ANN003, ARG001
        raise e2e.E2EError("cluster not reachable")

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr("olf.e2e.run", _raise)

    result = runner.invoke(app, ["e2e", "run", "--env", "local"])

    assert result.exit_code == 1
    assert "cluster not reachable" in result.output


def test_e2e_run_surfaces_a_toolchain_failure_from_contract_resolution_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A managed-toolchain provisioning failure (#127) raised while
    resolving the provider-contract environment - before e2e.run() and its
    own E2EError-only preflight are ever reached - must fail the same clean
    way as an E2EError, not escape as a raw ToolchainError traceback."""
    from olf import contracts as contracts_module
    from olf.deployment.errors import ToolchainError

    def _raise(terraform_dir, *, environ=None):  # noqa: ANN001, ANN202
        raise ToolchainError("terraform", reason="digest mismatch")

    monkeypatch.setattr(contracts_module, "load_provider_contracts", _raise)
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))

    result = runner.invoke(app, ["e2e", "run", "--env", "local"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, ToolchainError)
    assert "digest mismatch" in result.output


def test_e2e_run_rejects_a_profile_file_for_another_provider(tmp_path: Path) -> None:
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile, name="cloud-project", provider="aws", preset="slim", region="eu-west-3")

    result = runner.invoke(app, ["e2e", "run", "--env", "local", "-f", str(profile)])

    assert result.exit_code == 1
    assert "targets provider 'aws'" in result.output


def test_e2e_run_file_selects_the_topology_that_reaches_contract_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract_env_calls: list[dict]
) -> None:
    """`olf platform apply -f <profile>` records that profile's name in the v3
    contract, and `_parse_v3` refuses a contract parsed against a topology that
    disagrees - the nightly full e2e failure this option exists for. Asserting
    the resolved topology rather than the accepted flag is what makes reverting
    the forwarding fail this test.
    """
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    _write_profile(deployed / "openlakeforge.yaml", name="nightly-full", preset="full")
    _write_profile(tmp_path / "openlakeforge.yaml", name="project-default", preset="slim")

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr("olf.e2e.run", lambda *a, **k: None)

    result = runner.invoke(app, ["e2e", "run", "--env", "local", "-f", str(deployed / "openlakeforge.yaml")])

    assert result.exit_code == 0, result.output
    topology = contract_env_calls[0]["topology"]
    assert (topology.profile_name, topology.preset.value) == ("nightly-full", "full")
    assert contract_env_calls[0]["repo_root"] == deployed


def test_e2e_run_without_a_profile_file_keeps_resolving_the_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract_env_calls: list[dict]
) -> None:
    _write_profile(tmp_path / "openlakeforge.yaml", name="project-default", preset="slim")

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr("olf.e2e.run", lambda *a, **k: None)

    result = runner.invoke(app, ["e2e", "run", "--env", "local"])

    assert result.exit_code == 0, result.output
    assert contract_env_calls[0]["topology"].profile_name == "project-default"


def test_e2e_run_profile_shorthand_resolves_the_topology_olf_deploy_profile_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract_env_calls: list[dict]
) -> None:
    """`olf deploy --profile full` records the "legacy" topology in the v3
    contract; without the shorthand here, `olf e2e run` re-resolved the
    project profile and `_parse_v3` refused the name mismatch (#581)."""
    _write_profile(tmp_path / "openlakeforge.yaml", name="project-default", preset="slim")

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr("olf.e2e.run", lambda *a, **k: None)

    result = runner.invoke(app, ["e2e", "run", "--env", "local", "--profile", "full"])

    assert result.exit_code == 0, result.output
    topology = contract_env_calls[0]["topology"]
    assert (topology.profile_name, topology.preset.value) == ("legacy", "full")


def test_e2e_run_stage_selects_a_stage_of_the_profile_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract_env_calls: list[dict]
) -> None:
    from olf.profile import StageName

    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile, name="two-stage", preset="slim", stages=("dev", "prod"))

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    namespaces: list[str] = []
    monkeypatch.setattr("olf.e2e.run", lambda *a, **k: namespaces.append(k["namespace"]))

    result = runner.invoke(app, ["e2e", "run", "--env", "local", "-f", str(profile), "--stage", "prod"])

    assert result.exit_code == 0, result.output
    assert contract_env_calls[0]["stage"] == StageName.PROD
    assert namespaces == ["olf-prod"]


def test_e2e_run_rejects_a_stage_the_profile_file_does_not_enable(tmp_path: Path) -> None:
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile, name="dev-only", preset="slim")

    result = runner.invoke(app, ["e2e", "run", "--env", "local", "-f", str(profile), "--stage", "prod"])

    assert result.exit_code == 1
    assert "stage 'prod' is not enabled" in result.output
