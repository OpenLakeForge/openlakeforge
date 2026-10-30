from __future__ import annotations

import json
from pathlib import Path

import pytest
from _tooling_support import RecordingRunner

from olf.deployment.errors import CommandExecutionError
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver
from olf.tooling.terraform import Terraform


def _terraform(result: CommandResult | None = None) -> tuple[Terraform, RecordingRunner]:
    runner = RecordingRunner(result)
    resolver = PathExecutableResolver(overrides={"terraform": Path("terraform")})
    return Terraform(runner, resolver), runner


def test_output_raw_builds_exact_argv() -> None:
    terraform, runner = _terraform(
        CommandResult(argv=(), returncode=0, stdout="my-cluster\n", stderr="", duration_seconds=0.0)
    )
    terraform_dir = Path("/repo/infra/terraform/environments/local")

    value = terraform.output_raw(terraform_dir, "cluster_name")

    assert value == "my-cluster"
    assert runner.last_call.argv == [
        "terraform",
        f"-chdir={terraform_dir}",
        "output",
        "-raw",
        "cluster_name",
    ]


def test_output_json_parses_terraform_output() -> None:
    payload = {"a": 1, "b": [2, 3]}
    terraform, runner = _terraform(
        CommandResult(argv=(), returncode=0, stdout=json.dumps(payload), stderr="", duration_seconds=0.0)
    )

    value = terraform.output_json(Path("/repo/infra"), "provider_contracts")

    assert value == payload
    assert runner.last_call.argv[-3:] == ["output", "-json", "provider_contracts"]


def test_output_json_raises_typed_error_on_invalid_json() -> None:
    terraform, _runner = _terraform(
        CommandResult(argv=(), returncode=0, stdout="not json", stderr="", duration_seconds=0.0)
    )

    with pytest.raises(CommandExecutionError):
        terraform.output_json(Path("/repo/infra"), "provider_contracts")


def test_apply_builds_auto_approve_var_files_and_variables() -> None:
    terraform, runner = _terraform()
    terraform_dir = Path("/repo/infra/terraform/environments/local")

    terraform.apply(
        terraform_dir,
        var_files=[Path("/repo/local.tfvars")],
        variables={"namespace": "lakehouse", "kube_context": "kind-openlakeforge-local"},
        extra_args=["-parallelism=1"],
    )

    assert runner.last_call.argv == [
        "terraform",
        f"-chdir={terraform_dir}",
        "apply",
        "-auto-approve",
        "-var-file=/repo/local.tfvars",
        "-var=namespace=lakehouse",
        "-var=kube_context=kind-openlakeforge-local",
        "-parallelism=1",
    ]


def test_apply_without_auto_approve_omits_the_flag() -> None:
    terraform, runner = _terraform()

    terraform.apply(Path("/repo/infra"), auto_approve=False)

    assert "-auto-approve" not in runner.last_call.argv


def test_destroy_builds_expected_argv() -> None:
    terraform, runner = _terraform()
    terraform_dir = Path("/repo/infra")

    terraform.destroy(terraform_dir, variables={"namespace": "lakehouse"})

    assert runner.last_call.argv == [
        "terraform",
        f"-chdir={terraform_dir}",
        "destroy",
        "-auto-approve",
        "-var=namespace=lakehouse",
    ]


def test_state_show_defaults_to_check_false() -> None:
    terraform, runner = _terraform()

    terraform.state_show(Path("/repo/infra"), "kubernetes_namespace_v1.lakehouse")

    assert runner.last_call.kwargs["check"] is False
    assert runner.last_call.argv[-3:] == ["state", "show", "kubernetes_namespace_v1.lakehouse"]


def test_import_resource_builds_expected_argv() -> None:
    terraform, runner = _terraform()
    terraform_dir = Path("/repo/infra")

    terraform.import_resource(
        terraform_dir,
        "kubernetes_namespace_v1.lakehouse",
        "lakehouse",
        variables={"namespace": "lakehouse"},
    )

    assert runner.last_call.argv == [
        "terraform",
        f"-chdir={terraform_dir}",
        "import",
        "-var=namespace=lakehouse",
        "kubernetes_namespace_v1.lakehouse",
        "lakehouse",
    ]


def test_init_builds_expected_argv() -> None:
    terraform, runner = _terraform()
    terraform_dir = Path("/repo/infra/terraform/foundations/local-kind")

    terraform.init(terraform_dir)

    assert runner.last_call.argv == ["terraform", f"-chdir={terraform_dir}", "init"]


def test_installed_distribution_uses_external_state_data_and_readonly_lockfile(tmp_path: Path) -> None:
    terraform, runner = _terraform()
    terraform_dir = tmp_path / "payload/infra/terraform/environments/local"
    terraform_dir.mkdir(parents=True)
    (terraform_dir / ".terraform.lock.hcl").write_text("")
    env = {
        "OPENLAKEFORGE_TERRAFORM_DATA_ROOT": str(tmp_path / "work/terraform-data"),
        "OPENLAKEFORGE_TERRAFORM_STATE_ROOT": str(tmp_path / "state"),
        "OPENLAKEFORGE_TERRAFORM_READONLY_LOCKFILE": "true",
    }

    terraform.init(terraform_dir, env=env)
    assert runner.last_call.argv[-2:] == ["init", "-lockfile=readonly"]
    assert runner.last_call.kwargs["env"]["TF_DATA_DIR"] == str(tmp_path / "work/terraform-data/platform")

    terraform.state_show(terraform_dir, "kubernetes_namespace_v1.lakehouse", env=env)
    assert "-state=" + str(tmp_path / "state/platform.tfstate") in runner.last_call.argv


def test_installed_distribution_skips_readonly_lockfile_when_none_is_checked_in(tmp_path: Path) -> None:
    """`foundations/local-kind` declares no providers, so it has no
    `.terraform.lock.hcl` - `-lockfile=readonly` must not be added there, or
    Terraform rejects the init outright with no lock file to enforce.
    """
    terraform, runner = _terraform()
    terraform_dir = tmp_path / "payload/infra/terraform/foundations/local-kind"
    terraform_dir.mkdir(parents=True)
    env = {
        "OPENLAKEFORGE_TERRAFORM_DATA_ROOT": str(tmp_path / "work/terraform-data"),
        "OPENLAKEFORGE_TERRAFORM_STATE_ROOT": str(tmp_path / "state"),
        "OPENLAKEFORGE_TERRAFORM_READONLY_LOCKFILE": "true",
    }

    terraform.init(terraform_dir, env=env)

    assert runner.last_call.argv[-1:] == ["init"]


def test_apply_forwards_retry_policy_to_runner() -> None:
    from olf.tooling.retry import RetryPolicy

    terraform, runner = _terraform()
    policy = RetryPolicy(max_attempts=2, delay_seconds=0)

    terraform.apply(Path("/repo/infra"), retry_policy=policy)

    assert runner.last_call.kwargs["retry_policy"] is policy


def test_detailed_plan_allows_terraform_changes_exit_code() -> None:
    terraform, runner = _terraform(
        CommandResult(argv=("terraform", "plan"), returncode=2, stdout="changes", stderr="", duration_seconds=0.0)
    )

    result = terraform.plan(Path("/repo/infra"), detailed_exitcode=True)

    assert result.returncode == 2
    assert runner.last_call.kwargs["check"] is False
    assert "-detailed-exitcode" in runner.last_call.argv


def test_detailed_plan_raises_for_terraform_error() -> None:
    terraform, _runner = _terraform(
        CommandResult(argv=("terraform", "plan"), returncode=1, stdout="", stderr="invalid", duration_seconds=0.0)
    )

    with pytest.raises(CommandExecutionError, match=r"command failed \(1\)"):
        terraform.plan(Path("/repo/infra"), detailed_exitcode=True)
