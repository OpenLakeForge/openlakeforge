from __future__ import annotations

import contextlib
import socket
import tempfile
from pathlib import Path

from _tooling_support import RecordedCall, RecordingRunner

from olf.common.tooling.docker import Docker
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver


def _listening_socket(path: Path) -> socket.socket:
    """Bind and listen on a real AF_UNIX socket at `path`, under a short
    /tmp-rooted dir - macOS/BSD sun_path is capped at ~104 bytes, which
    pytest's deeply nested tmp_path can exceed once
    `.colima/<profile>/docker.sock` is appended. The caller must keep the
    returned socket alive (e.g. via `contextlib.closing`) for as long as it
    should be connectable - connectivity, not just the inode type, is what
    `resolve_current_engine_endpoint` now checks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def _dead_socket_file(path: Path) -> None:
    """Leave a socket-type inode at `path` with nothing listening - models a
    daemon (e.g. a quit Docker Desktop) that exited without unlinking its
    socket file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()


def _docker(result: CommandResult | None = None) -> tuple[Docker, RecordingRunner]:
    runner = RecordingRunner(result)
    resolver = PathExecutableResolver(overrides={"docker": Path("docker")})
    return Docker(runner, resolver), runner


def test_build_builds_exact_argv() -> None:
    docker, runner = _docker()

    docker.build(Path("/repo/images/project-code"), tag="project-code:local", build_args={"REVISION": "abc123"})

    assert runner.last_call.argv == [
        "docker",
        "build",
        "/repo/images/project-code",
        "-t",
        "project-code:local",
        "--build-arg",
        "REVISION=abc123",
    ]


def test_pull_and_push_build_expected_argv() -> None:
    docker, runner = _docker()

    docker.pull("ghcr.io/openlakeforge/project-code:local")
    assert runner.last_call.argv == ["docker", "pull", "ghcr.io/openlakeforge/project-code:local"]

    docker.push("ghcr.io/openlakeforge/project-code:local")
    assert runner.last_call.argv == ["docker", "push", "ghcr.io/openlakeforge/project-code:local"]


def test_pull_with_platform_inserts_platform_flag_before_image() -> None:
    docker, runner = _docker()

    docker.pull("python:3.12-slim", platform="linux/amd64")

    assert runner.last_call.argv == ["docker", "pull", "--platform", "linux/amd64", "python:3.12-slim"]


def test_tag_builds_expected_argv() -> None:
    docker, runner = _docker()

    docker.tag("project-code:local", "ghcr.io/openlakeforge/project-code:local")

    assert runner.last_call.argv == [
        "docker",
        "tag",
        "project-code:local",
        "ghcr.io/openlakeforge/project-code:local",
    ]


def test_login_uses_password_stdin_not_argv() -> None:
    docker, runner = _docker()

    docker.login("ghcr.io", username="foo", password="abc")

    assert runner.last_call.argv == ["docker", "login", "--username", "foo", "--password-stdin", "ghcr.io"]
    assert "abc" not in runner.last_call.argv
    assert runner.last_call.kwargs["input_text"] == "abc"


def test_context_show_and_inspect() -> None:
    docker, runner = _docker(CommandResult(argv=(), returncode=0, stdout="colima\n", stderr="", duration_seconds=0.0))

    name = docker.context_show()

    assert name == "colima"
    assert runner.last_call.argv == ["docker", "context", "show"]


def test_resolve_current_engine_endpoint_chains_show_and_inspect() -> None:
    class ScriptedRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if argv[-2:] == ["context", "show"]:
                stdout = "colima\n"
            else:
                stdout = "unix:///Users/dev/.colima/default/docker.sock\n"
            return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

    runner = ScriptedRunner()
    resolver = PathExecutableResolver(overrides={"docker": Path("docker")})
    docker = Docker(runner, resolver)

    endpoint = docker.resolve_current_engine_endpoint()

    assert endpoint == "unix:///Users/dev/.colima/default/docker.sock"
    assert runner.calls[0].argv == ["docker", "context", "show"]
    assert runner.calls[1].argv == [
        "docker",
        "context",
        "inspect",
        "colima",
        "--format",
        "{{.Endpoints.docker.Host}}",
    ]


def test_resolve_current_engine_endpoint_falls_back_to_default_colima_socket_when_resolution_fails() -> None:
    from olf.common.deployment.errors import CommandExecutionError

    class FailingRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            raise CommandExecutionError(["docker", "context", "show"], 1, stderr="no context")

    with tempfile.TemporaryDirectory(dir="/tmp") as home:
        default_socket = Path(home) / ".colima" / "default" / "docker.sock"
        with contextlib.closing(_listening_socket(default_socket)):
            docker = Docker(FailingRunner(), PathExecutableResolver(overrides={"docker": Path("docker")}))

            endpoint = docker.resolve_current_engine_endpoint(env={"HOME": home})

            assert endpoint == f"unix://{default_socket}"


def test_resolve_current_engine_endpoint_falls_back_when_resolved_socket_is_dead() -> None:
    """A daemon can exit without unlinking its socket file, so the endpoint
    `docker context inspect` reports can still look like a valid socket on
    disk while nothing actually accepts a connection - #156's exact failure
    mode with a stale Docker Desktop socket. `is_socket()` alone would miss
    this; connecting to it must not."""
    with tempfile.TemporaryDirectory(dir="/tmp") as home:
        stale_socket = Path(home) / "stale" / "docker.sock"
        _dead_socket_file(stale_socket)
        default_socket = Path(home) / ".colima" / "default" / "docker.sock"

        class ScriptedRunner(RecordingRunner):
            def run(self, command, **kwargs):  # type: ignore[override]
                argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                stdout = "default\n" if argv[-2:] == ["context", "show"] else f"unix://{stale_socket}\n"
                return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

        with contextlib.closing(_listening_socket(default_socket)):
            docker = Docker(ScriptedRunner(), PathExecutableResolver(overrides={"docker": Path("docker")}))

            endpoint = docker.resolve_current_engine_endpoint(env={"HOME": home})

            assert endpoint == f"unix://{default_socket}"


def test_resolve_current_engine_endpoint_ignores_a_dead_colima_candidate() -> None:
    """A dead socket file under `~/.colima` must not be treated as a usable
    fallback candidate - only a live, connectable one counts."""
    with tempfile.TemporaryDirectory(dir="/tmp") as home:
        stale_socket = Path(home) / "stale" / "docker.sock"
        _dead_socket_file(stale_socket)
        _dead_socket_file(Path(home) / ".colima" / "default" / "docker.sock")

        class ScriptedRunner(RecordingRunner):
            def run(self, command, **kwargs):  # type: ignore[override]
                argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                stdout = "default\n" if argv[-2:] == ["context", "show"] else f"unix://{stale_socket}\n"
                return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

        docker = Docker(ScriptedRunner(), PathExecutableResolver(overrides={"docker": Path("docker")}))

        endpoint = docker.resolve_current_engine_endpoint(env={"HOME": home})

        assert endpoint == f"unix://{stale_socket}"


def test_resolve_current_engine_endpoint_does_not_guess_between_ambiguous_colima_profiles() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as home:
        stale_socket = Path(home) / "stale" / "docker.sock"
        _dead_socket_file(stale_socket)

        class ScriptedRunner(RecordingRunner):
            def run(self, command, **kwargs):  # type: ignore[override]
                argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                stdout = "default\n" if argv[-2:] == ["context", "show"] else f"unix://{stale_socket}\n"
                return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

        profile_a = _listening_socket(Path(home) / ".colima" / "profileA" / "docker.sock")
        profile_b = _listening_socket(Path(home) / ".colima" / "profileB" / "docker.sock")
        with contextlib.closing(profile_a), contextlib.closing(profile_b):
            docker = Docker(ScriptedRunner(), PathExecutableResolver(overrides={"docker": Path("docker")}))

            endpoint = docker.resolve_current_engine_endpoint(env={"HOME": home})

            assert endpoint == f"unix://{stale_socket}"


def test_resolve_current_engine_endpoint_skips_colima_probe_when_resolved_socket_is_reachable() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as home:
        reachable_socket = Path(home) / "reachable" / "docker.sock"
        _dead_socket_file(Path(home) / ".colima" / "default" / "docker.sock")

        class ScriptedRunner(RecordingRunner):
            def run(self, command, **kwargs):  # type: ignore[override]
                argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                stdout = "default\n" if argv[-2:] == ["context", "show"] else f"unix://{reachable_socket}\n"
                return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

        with contextlib.closing(_listening_socket(reachable_socket)):
            docker = Docker(ScriptedRunner(), PathExecutableResolver(overrides={"docker": Path("docker")}))

            endpoint = docker.resolve_current_engine_endpoint(env={"HOME": home})

            assert endpoint == f"unix://{reachable_socket}"


def test_resolve_current_engine_endpoint_trusts_non_unix_schemes_without_validation() -> None:
    class ScriptedRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            stdout = "remote\n" if argv[-2:] == ["context", "show"] else "tcp://host:2375\n"
            return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

    docker = Docker(ScriptedRunner(), PathExecutableResolver(overrides={"docker": Path("docker")}))

    endpoint = docker.resolve_current_engine_endpoint()

    assert endpoint == "tcp://host:2375"


def test_server_arch_returns_stripped_output() -> None:
    docker, _runner = _docker(CommandResult(argv=(), returncode=0, stdout="arm64\n", stderr="", duration_seconds=0.0))

    assert docker.server_arch() == "arm64"


def test_server_arch_returns_empty_string_on_failure() -> None:
    from olf.common.deployment.errors import CommandExecutionError

    class FailingRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            raise CommandExecutionError(["docker", "version"], 1, stderr="daemon not running")

    docker = Docker(FailingRunner(), PathExecutableResolver(overrides={"docker": Path("docker")}))

    assert docker.server_arch() == ""


def test_image_exists_reflects_inspect_exit_code() -> None:
    result = CommandResult(argv=(), returncode=1, stdout="", stderr="no such image", duration_seconds=0.0)
    docker, _runner = _docker(result)

    assert docker.image_exists("apache/polaris:1.4.0") is False


def test_image_inspect_defaults_to_check_false() -> None:
    docker, runner = _docker()

    docker.image_inspect("apache/polaris:1.4.0")

    assert runner.last_call.kwargs["check"] is False
    assert runner.last_call.argv == ["docker", "image", "inspect", "apache/polaris:1.4.0"]


def test_save_builds_exact_argv() -> None:
    docker, runner = _docker()

    docker.save("apache/polaris:1.4.0", output_path=Path("/tmp/work/polaris.tar"))

    assert runner.last_call.argv == ["docker", "save", "apache/polaris:1.4.0", "-o", "/tmp/work/polaris.tar"]


def test_exec_builds_privileged_interactive_argv_with_stdin_path() -> None:
    docker, runner = _docker()

    docker.exec_(
        "openlakeforge-local-control-plane",
        ["ctr", "--namespace=k8s.io", "images", "import", "--snapshotter=overlayfs", "-"],
        privileged=True,
        interactive=True,
        stdin_path=Path("/tmp/work/polaris.tar"),
    )

    assert runner.last_call.argv == [
        "docker",
        "exec",
        "--privileged",
        "-i",
        "openlakeforge-local-control-plane",
        "ctr",
        "--namespace=k8s.io",
        "images",
        "import",
        "--snapshotter=overlayfs",
        "-",
    ]
    assert runner.last_call.kwargs["stdin_path"] == Path("/tmp/work/polaris.tar")


def test_exec_without_privileged_or_interactive() -> None:
    docker, runner = _docker()

    docker.exec_("node", ["crictl", "inspecti", "apache/polaris:1.4.0"])

    assert runner.last_call.argv == ["docker", "exec", "node", "crictl", "inspecti", "apache/polaris:1.4.0"]


def test_resolve_current_engine_endpoint_returns_none_on_failure() -> None:
    from olf.common.deployment.errors import CommandExecutionError

    class FailingRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            raise CommandExecutionError(["docker", "context", "show"], 1, stderr="no docker")

    runner = FailingRunner()
    resolver = PathExecutableResolver(overrides={"docker": Path("docker")})
    docker = Docker(runner, resolver)

    assert docker.resolve_current_engine_endpoint() is None


def test_resolve_current_engine_endpoint_returns_none_when_docker_is_not_installed() -> None:
    from olf.common.deployment.errors import ExecutableNotFoundError

    class MissingDockerRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            raise ExecutableNotFoundError("docker")

    runner = MissingDockerRunner()
    resolver = PathExecutableResolver(overrides={"docker": Path("docker")})
    docker = Docker(runner, resolver)

    assert docker.resolve_current_engine_endpoint() is None
