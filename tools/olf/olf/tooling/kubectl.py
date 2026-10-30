"""Thin kubectl adapter: primitives only, no cleanup/import lifecycle logic.

Every cluster-scoped call takes `context`/`kubeconfig`/`namespace` explicitly
so behavior never depends on the caller's ambient kubectl context.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from olf.deployment.errors import CommandExecutionError, ToolingError
from olf.tooling.process import CommandResult, ProcessRunner
from olf.tooling.resolver import ExecutableResolver
from olf.tooling.retry import RetryPolicy, RetryPredicate


class KubeContextUnreachableError(ToolingError):
    def __init__(self, context: str, *, reason: str) -> None:
        self.context = context
        self.reason = reason
        super().__init__(f"Kubernetes context '{context}' is not reachable: {reason}")


class Kubectl:
    def __init__(self, runner: ProcessRunner, resolver: ExecutableResolver) -> None:
        self._runner = runner
        self._resolver = resolver

    def _executable(self) -> Path:
        return self._resolver.resolve("kubectl")

    def _run(
        self,
        args: Sequence[str],
        *,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        timeout_seconds: float | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        argv = [str(self._executable())]
        if context is not None:
            argv += ["--context", context]
        argv.extend(args)

        merged_env: dict[str, str] = dict(env or {})
        if kubeconfig is not None:
            merged_env["KUBECONFIG"] = str(kubeconfig)

        return self._runner.run(
            argv,
            env=merged_env or None,
            check=check,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            retry_if=retry_if,
            input_text=input_text,
        )

    def create_from_manifest(
        self,
        manifest: str,
        *,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """`kubectl create -f -`: the manifest travels on stdin, so a Secret's data never appears in argv."""
        return self._run(["create", "-f", "-"], context=context, kubeconfig=kubeconfig, env=env, input_text=manifest)

    def apply_from_manifest(
        self,
        manifest: str,
        *,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """`kubectl apply --server-side -f -`: create or replace from stdin. Server-side apply keeps a Secret's
        data out of the last-applied annotation that client-side apply would write."""
        return self._run(
            ["apply", "--server-side", "--force-conflicts", "-f", "-"],
            context=context,
            kubeconfig=kubeconfig,
            env=env,
            input_text=manifest,
        )

    def config_get_contexts(
        self,
        *,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        result = self._run(["config", "get-contexts", "-o", "name"], kubeconfig=kubeconfig, env=env)
        return [line for line in result.stdout.splitlines() if line]

    def cluster_info(
        self,
        *,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        return self._run(["cluster-info"], context=context, kubeconfig=kubeconfig, env=env, check=check)

    def get(
        self,
        resource: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        context: str | None = None,
        kubeconfig: Path | None = None,
        output: str | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        args = ["get", resource]
        if name is not None:
            args.append(name)
        if namespace is not None:
            args += ["-n", namespace]
        if output is not None:
            args += ["-o", output]
        args.extend(extra_args)
        return self._run(args, context=context, kubeconfig=kubeconfig, env=env, check=check)

    def delete(
        self,
        resource: str,
        name: str,
        *,
        namespace: str | None = None,
        context: str | None = None,
        kubeconfig: Path | None = None,
        ignore_not_found: bool = True,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["delete", resource, name]
        if namespace is not None:
            args += ["-n", namespace]
        if ignore_not_found:
            args.append("--ignore-not-found")
        args.extend(extra_args)
        return self._run(args, context=context, kubeconfig=kubeconfig, env=env)

    def exec(
        self,
        pod: str,
        command: Sequence[str],
        *,
        namespace: str,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return self._run(
            ["exec", pod, "-n", namespace, "--", *command], context=context, kubeconfig=kubeconfig, env=env
        )

    def patch(
        self,
        resource: str,
        name: str,
        patch: str,
        *,
        namespace: str | None = None,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """JSON merge patch."""
        args = ["patch", resource, name, "--type", "merge", "-p", patch]
        if namespace is not None:
            args += ["-n", namespace]
        return self._run(args, context=context, kubeconfig=kubeconfig, env=env)

    def rollout_status(
        self,
        resource: str,
        *,
        namespace: str | None = None,
        context: str | None = None,
        kubeconfig: Path | None = None,
        timeout: str | None = None,
        selector: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["rollout", "status", resource]
        if namespace is not None:
            args += ["-n", namespace]
        if selector is not None:
            args += ["-l", selector]
        if timeout is not None:
            args += ["--timeout", timeout]
        return self._run(args, context=context, kubeconfig=kubeconfig, env=env)

    def rollout_restart(
        self,
        resource: str,
        *,
        namespace: str,
        selector: str | None = None,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["rollout", "restart", resource, "-n", namespace]
        if selector is not None:
            args += ["-l", selector]
        return self._run(args, context=context, kubeconfig=kubeconfig, env=env)

    def wait(
        self,
        resource: str,
        *,
        for_condition: str,
        namespace: str | None = None,
        context: str | None = None,
        kubeconfig: Path | None = None,
        timeout: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["wait", resource, f"--for={for_condition}"]
        if namespace is not None:
            args += ["-n", namespace]
        if timeout is not None:
            args += ["--timeout", timeout]
        return self._run(args, context=context, kubeconfig=kubeconfig, env=env)

    def config_user(
        self, *, context: str, kubeconfig: Path | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        """The kubeconfig user behind `context`."""
        jsonpath = "jsonpath={.contexts[0].context.user}"
        args = ["config", "view", "--minify", "-o", jsonpath]
        return self._run(args, context=context, kubeconfig=kubeconfig, env=env)

    def exec_shell_argv(self, resource: str, *, namespace: str, context: str) -> list[str]:
        """An interactive `sh` in the resource's pod, for a caller that owns the process."""
        argv = [str(self._executable()), "--context", context, "exec", "-it", resource, "-n", namespace, "--", "sh"]
        return argv

    def port_forward_argv(
        self,
        resource: str,
        ports: Sequence[str],
        *,
        namespace: str | None = None,
        context: str | None = None,
    ) -> list[str]:
        """Build the full `kubectl [--context ...] port-forward ...` argv.

        Exposed separately from `port_forward` so callers that need a
        long-lived, backgrounded child process (see
        `olf.deployment.portforward.PortForwardSupervisor`) can build the
        exact argv without going through this adapter's blocking call.
        """
        argv = [str(self._executable())]
        if context is not None:
            argv += ["--context", context]
        # Loopback only: a quickstart has no authentication in front of these services.
        argv += ["port-forward", resource, *ports, "--address", "127.0.0.1"]
        if namespace is not None:
            argv += ["-n", namespace]
        return argv

    def port_forward(
        self,
        resource: str,
        ports: Sequence[str],
        *,
        namespace: str | None = None,
        context: str | None = None,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        args = ["port-forward", resource, *ports]
        if namespace is not None:
            args += ["-n", namespace]
        return self._run(
            args,
            context=context,
            kubeconfig=kubeconfig,
            env=env,
            timeout_seconds=timeout_seconds,
        )

    def require_context_reachable(
        self,
        context: str,
        *,
        kubeconfig: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        contexts = self.config_get_contexts(kubeconfig=kubeconfig, env=env)
        if context not in contexts:
            raise KubeContextUnreachableError(
                context,
                reason=f"absent from kubeconfig {kubeconfig if kubeconfig is not None else '(default)'}",
            )
        try:
            self.cluster_info(context=context, kubeconfig=kubeconfig, env=env)
        except CommandExecutionError as exc:
            raise KubeContextUnreachableError(context, reason=str(exc)) from exc
