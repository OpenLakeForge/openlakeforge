"""Thin Terraform adapter: structured argv only, no HCL/state logic in Python."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from olf.deployment.errors import CommandExecutionError
from olf.tooling.process import CommandResult, ProcessRunner
from olf.tooling.resolver import ExecutableResolver
from olf.tooling.retry import RetryPolicy, RetryPredicate


def external_state_options(
    terraform_dir: Path, environ: Mapping[str, str], *, create: bool = True
) -> tuple[dict[str, str], Path | None]:
    """Derive the `TF_DATA_DIR` overlay and `-state=<path>` value for state and
    plugin data rooted outside `terraform_dir` (installed distributions).

    Every direct Terraform invocation - `Terraform._run` and any caller that
    shells out to `terraform` on its own (`olf.contract_state.load_provider_contracts`,
    `olf.e2e._shell.terraform_output`) - must resolve state and data roots
    identically, or an installed deployment's `-chdir`'d `terraform output`
    silently reads the read-only payload's default (absent) state instead of
    the one `terraform apply` wrote under `OLF_HOME`.

    Returns `({}, None)` when either `OPENLAKEFORGE_TERRAFORM_DATA_ROOT` or
    `OPENLAKEFORGE_TERRAFORM_STATE_ROOT` is unset, matching source-mode /
    non-distribution runs where Terraform's own directory-relative defaults
    apply. `create=False` skips creating the directories, for read-only
    callers (contract/output reads) that must not conjure state directories
    that a prior `apply` never created.
    """
    data_root = environ.get("OPENLAKEFORGE_TERRAFORM_DATA_ROOT")
    state_root = environ.get("OPENLAKEFORGE_TERRAFORM_STATE_ROOT")
    if not data_root or not state_root:
        return {}, None
    group = terraform_dir.parent.name
    scope = "foundation" if group == "foundations" else "platform"
    data_dir = Path(data_root) / scope
    state_path = Path(state_root) / f"{scope}.tfstate"
    if create:
        data_dir.mkdir(parents=True, exist_ok=True)
        state_path.parent.mkdir(parents=True, exist_ok=True)
    return {"TF_DATA_DIR": str(data_dir)}, state_path


class Terraform:
    def __init__(self, runner: ProcessRunner, resolver: ExecutableResolver) -> None:
        self._runner = runner
        self._resolver = resolver

    def _executable(self) -> Path:
        return self._resolver.resolve("terraform")

    def _run(
        self,
        terraform_dir: Path,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
        stream_output: bool = False,
    ) -> CommandResult:
        command_env = dict(env or {})
        command_args = list(args)
        overlay, state_path = external_state_options(terraform_dir, command_env)
        if state_path is not None:
            command_env.update(overlay)
            if command_args and command_args[0] == "init":
                if command_env.get(
                    "OPENLAKEFORGE_TERRAFORM_READONLY_LOCKFILE"
                ) == "true" and (terraform_dir / ".terraform.lock.hcl").is_file():
                    # Terraform rejects `-lockfile=readonly` outright when no
                    # dependency lock file exists yet - true for
                    # `foundations/local-kind`, which declares no providers
                    # at all (only the built-in `terraform_data` resource).
                    command_args.append("-lockfile=readonly")
            else:
                insert_at = 2 if command_args[:1] == ["state"] else 1
                command_args.insert(insert_at, f"-state={state_path}")
        argv = [str(self._executable()), f"-chdir={terraform_dir}", *command_args]
        return self._runner.run(
            argv,
            env=command_env or None,
            check=check,
            retry_policy=retry_policy,
            retry_if=retry_if,
            stream_output=stream_output,
        )

    @staticmethod
    def _var_args(var_files: Sequence[Path | str], variables: Mapping[str, str] | None) -> list[str]:
        args: list[str] = [f"-var-file={var_file}" for var_file in var_files]
        for key, value in (variables or {}).items():
            args.append(f"-var={key}={value}")
        return args

    def init(
        self,
        terraform_dir: Path,
        *,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return self._run(terraform_dir, ["init", *extra_args], env=env, stream_output=True)

    def apply(
        self,
        terraform_dir: Path,
        *,
        auto_approve: bool = True,
        var_files: Sequence[Path | str] = (),
        variables: Mapping[str, str] | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
    ) -> CommandResult:
        args = ["apply"]
        if auto_approve:
            args.append("-auto-approve")
        args.extend(self._var_args(var_files, variables))
        args.extend(extra_args)
        return self._run(
            terraform_dir, args, env=env, retry_policy=retry_policy, retry_if=retry_if, stream_output=True
        )

    def plan(
        self,
        terraform_dir: Path,
        *,
        var_files: Sequence[Path | str] = (),
        variables: Mapping[str, str] | None = None,
        detailed_exitcode: bool = False,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Create a non-mutating Terraform plan.

        Terraform uses exit code 2 to report pending changes when
        ``-detailed-exitcode`` is supplied.  That is a successful plan, so
        callers always receive the result and decide whether to expose 2.
        """
        args = ["plan", "-input=false"]
        if detailed_exitcode:
            args.append("-detailed-exitcode")
        args.extend(self._var_args(var_files, variables))
        args.extend(extra_args)
        result = self._run(terraform_dir, args, env=env, check=not detailed_exitcode, stream_output=True)
        if detailed_exitcode and result.returncode not in (0, 2):
            raise CommandExecutionError(result.argv, result.returncode, stdout=result.stdout, stderr=result.stderr)
        return result

    def destroy(
        self,
        terraform_dir: Path,
        *,
        auto_approve: bool = True,
        var_files: Sequence[Path | str] = (),
        variables: Mapping[str, str] | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
    ) -> CommandResult:
        args = ["destroy"]
        if auto_approve:
            args.append("-auto-approve")
        args.extend(self._var_args(var_files, variables))
        args.extend(extra_args)
        return self._run(
            terraform_dir, args, env=env, retry_policy=retry_policy, retry_if=retry_if, stream_output=True
        )

    def output_raw(self, terraform_dir: Path, name: str, *, env: Mapping[str, str] | None = None) -> str:
        result = self._run(terraform_dir, ["output", "-raw", name], env=env)
        return result.stdout.strip()

    def output_json(self, terraform_dir: Path, name: str, *, env: Mapping[str, str] | None = None) -> Any:
        result = self._run(terraform_dir, ["output", "-json", name], env=env)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CommandExecutionError(
                result.argv,
                result.returncode,
                stdout=result.stdout,
                stderr=f"terraform output {name!r} did not return valid JSON",
            ) from exc

    def state_show(
        self,
        terraform_dir: Path,
        resource_addr: str,
        *,
        env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> CommandResult:
        return self._run(terraform_dir, ["state", "show", resource_addr], env=env, check=check)

    def import_resource(
        self,
        terraform_dir: Path,
        resource_addr: str,
        resource_id: str,
        *,
        var_files: Sequence[Path | str] = (),
        variables: Mapping[str, str] | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["import", *self._var_args(var_files, variables), *extra_args, resource_addr, resource_id]
        return self._run(terraform_dir, args, env=env)
