"""The one process-execution API for new deployment code.

Every command is executed as structured argv (`shell=False`); nothing here
ever builds or interprets a shell command string.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from olf.deployment.errors import CommandExecutionError, CommandTimeoutError, ExecutableNotFoundError
from olf.tooling.redact import redact_argv
from olf.tooling.retry import RetryPolicy, RetryPredicate, run_with_retry


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    timeout_seconds: float | None = None
    input_text: str | None = None
    stdin_path: Path | None = None

    def __post_init__(self) -> None:
        argv = tuple(str(part) for part in self.argv)
        if not argv:
            raise ValueError("Command.argv must not be empty")
        object.__setattr__(self, "argv", argv)
        if self.input_text is not None and self.stdin_path is not None:
            raise ValueError("Command.input_text and Command.stdin_path are mutually exclusive")


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _to_command(
    command: Command | Sequence[str | Path],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    timeout_seconds: float | None,
    input_text: str | None,
    stdin_path: Path | None,
) -> Command:
    if isinstance(command, Command):
        return command
    return Command(
        argv=tuple(str(part) for part in command),
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        input_text=input_text,
        stdin_path=stdin_path,
    )


class ProcessRunner:
    """Executes `Command`s with `subprocess.run`, no shell involvement.

    `env`, when given, is layered on top of a copy of the current process
    environment (never `os.environ` itself) so callers only need to specify
    the deployment-scoped overrides they care about, while the parent
    process's environment is left untouched.
    """

    def __init__(self, *, log_commands: bool = False) -> None:
        self._log_commands = log_commands

    def run(
        self,
        command: Command | Sequence[str | Path],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        input_text: str | None = None,
        stdin_path: Path | None = None,
        check: bool = True,
        retry_policy: RetryPolicy | None = None,
        retry_if: RetryPredicate | None = None,
        stream_output: bool = False,
    ) -> CommandResult:
        cmd = _to_command(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            input_text=input_text,
            stdin_path=stdin_path,
        )

        def attempt() -> CommandResult:
            return self._run_once(cmd, check=check, stream_output=stream_output)

        if retry_policy is not None:
            return run_with_retry(attempt, policy=retry_policy, retry_if=retry_if)
        return attempt()

    def _run_once(self, command: Command, *, check: bool, stream_output: bool = False) -> CommandResult:
        argv = list(command.argv)
        full_env = {**os.environ, **command.env} if command.env is not None else None

        if self._log_commands:
            from olf import log

            sanitized = " ".join(redact_argv(argv))
            log.step(f"run: {sanitized}")

        started = time.perf_counter()
        stdin_file = open(command.stdin_path, "rb") if command.stdin_path is not None else None  # noqa: SIM115
        try:
            if stream_output:
                returncode, stdout_text, stderr_text = self._run_streaming(
                    argv, cwd=command.cwd, env=full_env, timeout_seconds=command.timeout_seconds
                )
            else:
                completed = subprocess.run(  # noqa: S603 - argv is structured, shell=False
                    argv,
                    cwd=command.cwd,
                    env=full_env,
                    capture_output=True,
                    text=True,
                    timeout=command.timeout_seconds,
                    input=command.input_text,
                    stdin=(stdin_file if stdin_file is not None else subprocess.DEVNULL)
                    if command.input_text is None
                    else None,
                    check=False,
                    shell=False,
                )
                returncode = completed.returncode
                stdout_text = completed.stdout or ""
                stderr_text = completed.stderr or ""
        except FileNotFoundError as exc:
            raise ExecutableNotFoundError(argv[0]) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(tuple(argv), command.timeout_seconds or 0.0) from exc
        finally:
            if stdin_file is not None:
                stdin_file.close()

        duration = time.perf_counter() - started
        result = CommandResult(
            argv=tuple(argv),
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_seconds=duration,
        )

        if check and result.returncode != 0:
            raise CommandExecutionError(
                result.argv,
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                cwd=command.cwd,
                env=command.env,
            )
        return result

    @staticmethod
    def _run_streaming(
        argv: list[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float | None,
    ) -> tuple[int, str, str]:
        """Run a command with its stdout/stderr echoed live to the terminal.

        `subprocess.run(capture_output=True)` buffers everything until exit,
        which hides Terraform's per-resource apply/destroy progress for the
        whole duration of a foundation/platform operation - the removed
        shell scripts let Terraform inherit the terminal directly. This
        tees each stream to the real terminal as it arrives while still
        collecting it for `CommandResult`.
        """
        process = subprocess.Popen(  # noqa: S603 - argv is structured, shell=False
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=os.name == "posix",
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _pump(source, sink, dest_stream) -> None:  # noqa: ANN001
            for line in iter(source.readline, ""):
                dest_stream.write(line)
                dest_stream.flush()
                sink.append(line)
            source.close()

        stdout_thread = threading.Thread(target=_pump, args=(process.stdout, stdout_lines, sys.stdout))
        stderr_thread = threading.Thread(target=_pump, args=(process.stderr, stderr_lines, sys.stderr))
        stdout_thread.start()
        stderr_thread.start()

        try:
            returncode = process.wait(timeout=timeout_seconds)
        except BaseException:
            # A caller may enforce a deadline with a signal (as `olf smoke`
            # does), which interrupts `wait()` with its own exception rather
            # than subprocess.TimeoutExpired. Stop the child before joining
            # the pipe readers so a hung command cannot keep those pipes open
            # and defeat the enclosing deadline.
            _terminate_streaming_process(process)
            process.wait()
            raise
        finally:
            stdout_thread.join()
            stderr_thread.join()

        return returncode, "".join(stdout_lines), "".join(stderr_lines)


def _terminate_streaming_process(process) -> None:  # noqa: ANN001
    """Stop a streamed child and its descendants without involving a shell."""
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (AttributeError, ProcessLookupError):
            pass
    process.kill()
