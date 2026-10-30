"""Thin Docker adapter.

Preserves the "resolve the selected engine endpoint before scoping
DOCKER_CONFIG" behavior from `scripts/lib/common.sh::configure_deployment_scope`
so an isolated DOCKER_CONFIG cannot silently fall back to
`/var/run/docker.sock` on hosts using Colima or another non-default context.
When that resolution itself misses (e.g. it returns a stale Docker Desktop
socket on a Colima-only host - #156), `resolve_current_engine_endpoint` falls
back to probing Colima's own on-disk socket convention.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path

from olf.common.deployment.errors import CommandExecutionError, ExecutableNotFoundError
from olf.common.tooling.process import CommandResult, ProcessRunner
from olf.common.tooling.resolver import ExecutableResolver
from olf.common.tooling.retry import RetryPolicy, RetryPredicate

_SOCKET_CONNECT_TIMEOUT_SECONDS = 0.5


def _unix_socket_connectable(path: Path) -> bool:
    """Whether a unix socket at `path` actually accepts a connection.

    A daemon that exited without unlinking its socket can leave the inode
    behind - `Path.is_socket()` stays true even though connecting fails with
    ECONNREFUSED - so connectivity must be tested directly rather than
    inferred from the file type alone.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_SOCKET_CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


def _is_reachable_unix_endpoint(endpoint: str) -> bool:
    """Whether a `unix://` endpoint's socket actually accepts a connection.

    Non-unix schemes (tcp://, npipe://, ssh://) are trusted without validation -
    remote/Windows engines are out of scope here. Catches the #156 failure
    mode: a stale Docker Desktop socket at the default endpoint that
    `docker context inspect` still reports, even though nothing listens on it.
    """
    if not endpoint.startswith("unix://"):
        return True
    return _unix_socket_connectable(Path(endpoint.removeprefix("unix://")))


def _probe_colima_socket(*, env: Mapping[str, str] | None) -> str | None:
    """Look for a Colima-managed Docker socket under `$HOME/.colima`.

    Only probes when `env` carries an explicit HOME - deliberately never
    falls back to `Path.home()`, which would make this depend on whichever
    machine runs the code, including tests that pass a HOME-less env to stay
    isolated from ambient state. Prefers the `default` profile; an
    unambiguous single non-default profile is used as-is; two or more
    candidates are not guessed between.
    """
    home = env.get("HOME") if env else None
    if not home:
        return None
    colima_dir = Path(home) / ".colima"
    if not colima_dir.is_dir():
        return None
    candidates = sorted(p for p in colima_dir.glob("*/docker.sock") if _unix_socket_connectable(p))
    if not candidates:
        return None
    default_socket = colima_dir / "default" / "docker.sock"
    if default_socket in candidates:
        return f"unix://{default_socket}"
    if len(candidates) == 1:
        return f"unix://{candidates[0]}"
    return None


def ambient_registry_env(env: Mapping[str, str]) -> dict[str, str]:
    """Swap OpenLakeForge's scoped Docker config for the caller's own.

    A provider's command environment points `DOCKER_CONFIG` at an OLF-owned
    directory so build state stays isolated, and the only credentials ever
    written there are a provider registry login. An image in a private GHCR or
    Docker Hub repository -- the registry the local workflow documents -- is
    reachable only with what the developer's own `docker login` wrote, so any
    operation against a registry OLF does not own has to use their config.
    """
    merged = dict(env)
    ambient = os.environ.get("DOCKER_CONFIG")
    if ambient:
        merged["DOCKER_CONFIG"] = ambient
    else:
        merged.pop("DOCKER_CONFIG", None)
    return merged


class Docker:
    def __init__(self, runner: ProcessRunner, resolver: ExecutableResolver) -> None:
        self._runner = runner
        self._resolver = resolver

    def _executable(self) -> Path:
        return self._resolver.resolve("docker")

    def _run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
        stdin_path: Path | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
    ) -> CommandResult:
        argv = [str(self._executable()), *args]
        return self._runner.run(
            argv,
            env=env,
            check=check,
            input_text=input_text,
            stdin_path=stdin_path,
            retry_policy=retry_policy,
            retry_if=retry_if,
        )

    def version(self, *, format_template: str | None = None, env: Mapping[str, str] | None = None) -> CommandResult:
        args = ["version"]
        if format_template is not None:
            args += ["--format", format_template]
        return self._run(args, env=env, check=True)

    def server_arch(self, *, env: Mapping[str, str] | None = None) -> str:
        try:
            result = self.version(format_template="{{.Server.Arch}}", env=env)
        except CommandExecutionError:
            return ""
        return result.stdout.strip()

    def image_inspect(
        self,
        image: str,
        *,
        env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> CommandResult:
        return self._run(["image", "inspect", image], env=env, check=check)

    def image_exists(self, image: str, *, env: Mapping[str, str] | None = None) -> bool:
        return self.image_inspect(image, env=env, check=False).ok

    def save(self, image: str, *, output_path: Path, env: Mapping[str, str] | None = None) -> CommandResult:
        return self._run(["save", image, "-o", str(output_path)], env=env)

    def exec_(
        self,
        container: str,
        args: Sequence[str],
        *,
        privileged: bool = False,
        interactive: bool = False,
        stdin_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        exec_args = ["exec"]
        if privileged:
            exec_args.append("--privileged")
        if interactive:
            exec_args.append("-i")
        exec_args.append(container)
        exec_args.extend(args)
        return self._run(exec_args, env=env, check=check, stdin_path=stdin_path)

    def context_show(self, *, env: Mapping[str, str] | None = None) -> str:
        return self._run(["context", "show"], env=env).stdout.strip()

    def context_inspect(
        self,
        name: str,
        *,
        format_template: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        args = ["context", "inspect", name]
        if format_template is not None:
            args += ["--format", format_template]
        return self._run(args, env=env).stdout.strip()

    def resolve_current_engine_endpoint(self, *, env: Mapping[str, str] | None = None) -> str | None:
        """Resolve `.Endpoints.docker.Host` for the currently selected context.

        Returns None (rather than raising) when Docker isn't usable, mirroring
        the shell helper's `command -v docker &>/dev/null` guard plus `|| true`
        fallback — callers keep any explicit DOCKER_HOST override in that
        case. This must tolerate Docker being entirely absent (not just a
        failing command), since Docker-independent operations (status,
        platform/foundation teardown) also resolve a command environment.

        When the resolved endpoint isn't actually reachable - #156's stale
        Docker Desktop socket surviving under a scoped, context-less
        DOCKER_CONFIG on a Colima-only host - probes Colima's on-disk socket
        convention rather than handing the caller a dead endpoint. Falls back
        to the original (possibly unreachable) result when the probe can't
        disambiguate, so an already-correct non-Colima setup is never touched.
        """
        try:
            current = self.context_show(env=env)
            endpoint = self.context_inspect(current, format_template="{{.Endpoints.docker.Host}}", env=env) or None
        except (CommandExecutionError, ExecutableNotFoundError):
            endpoint = None

        if endpoint is not None and _is_reachable_unix_endpoint(endpoint):
            return endpoint
        return _probe_colima_socket(env=env) or endpoint

    def build(
        self,
        context_dir: Path,
        *,
        tag: str | None = None,
        file: Path | None = None,
        build_args: Mapping[str, str] | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
    ) -> CommandResult:
        args = ["build", str(context_dir)]
        if tag is not None:
            args += ["-t", tag]
        if file is not None:
            args += ["-f", str(file)]
        for key, value in (build_args or {}).items():
            args += ["--build-arg", f"{key}={value}"]
        args.extend(extra_args)
        return self._run(args, env=env, retry_policy=retry_policy, retry_if=retry_if)

    def pull(
        self,
        image: str,
        *,
        platform: str | None = None,
        env: Mapping[str, str] | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
    ) -> CommandResult:
        args = ["pull"]
        if platform is not None:
            args += ["--platform", platform]
        args.append(image)
        return self._run(args, env=env, retry_policy=retry_policy, retry_if=retry_if)

    def push(
        self,
        image: str,
        *,
        env: Mapping[str, str] | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
    ) -> CommandResult:
        return self._run(["push", image], env=env, retry_policy=retry_policy, retry_if=retry_if)

    def load(self, *, input_path: Path | None = None, env: Mapping[str, str] | None = None) -> CommandResult:
        args = ["load"]
        if input_path is not None:
            args += ["-i", str(input_path)]
        return self._run(args, env=env)

    def tag(self, source: str, target: str, *, env: Mapping[str, str] | None = None) -> CommandResult:
        return self._run(["tag", source, target], env=env)

    def login(
        self,
        registry: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["login"]
        if username is not None:
            args += ["--username", username]
        if password is not None:
            args.append("--password-stdin")
        if registry is not None:
            args.append(registry)
        return self._run(args, env=env, input_text=password)
