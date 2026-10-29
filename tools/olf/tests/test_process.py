from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path

import pytest
from _tooling_support import write_fake_executable, write_sleeping_executable

from olf.deployment.errors import CommandExecutionError, CommandTimeoutError, ExecutableNotFoundError
from olf.tooling.process import Command, CommandResult, ProcessRunner
from olf.tooling.retry import RetryPolicy


def test_successful_command_captures_output_and_duration(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake")
    runner = ProcessRunner()

    result = runner.run([str(script), "hello", "world"])

    assert isinstance(result, CommandResult)
    assert result.returncode == 0
    assert result.duration_seconds >= 0
    assert result.argv == (str(script), "hello", "world")
    payload = json.loads(result.stdout)
    assert payload["argv"] == ["hello", "world"]


def test_failed_command_raises_typed_error_with_sanitized_argv(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake", stderr="boom", exit_code=3)
    runner = ProcessRunner()

    with pytest.raises(CommandExecutionError) as excinfo:
        runner.run([str(script), "--password", "super-secret"])

    error = excinfo.value
    assert error.returncode == 3
    assert "super-secret" not in str(error)
    assert "<redacted>" in str(error)
    assert error.argv[-2:] == ("--password", "super-secret")  # original argv retained on the error object


def test_check_false_returns_failed_result_instead_of_raising(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake", exit_code=7)
    runner = ProcessRunner()

    result = runner.run([str(script)], check=False)

    assert result.returncode == 7


def test_missing_executable_raises_typed_error(tmp_path: Path) -> None:
    runner = ProcessRunner()

    with pytest.raises(ExecutableNotFoundError) as excinfo:
        runner.run([str(tmp_path / "does-not-exist")])

    assert excinfo.value.tool == str(tmp_path / "does-not-exist")


def test_timeout_raises_typed_error(tmp_path: Path) -> None:
    script = write_sleeping_executable(tmp_path / "slow", seconds=5)
    runner = ProcessRunner()

    with pytest.raises(CommandTimeoutError):
        runner.run([str(script)], timeout_seconds=0.05)


def test_stream_output_echoes_live_and_still_captures_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "fake.sh"
    script.write_text("#!/usr/bin/env bash\necho out-line\necho err-line 1>&2\n")
    script.chmod(0o755)
    runner = ProcessRunner()

    result = runner.run([str(script)], stream_output=True)

    assert result.returncode == 0
    assert result.stdout == "out-line\n"
    assert result.stderr == "err-line\n"
    captured = capsys.readouterr()
    assert "out-line" in captured.out
    assert "err-line" in captured.err


def test_stream_output_failed_command_raises_typed_error(tmp_path: Path) -> None:
    script = tmp_path / "fake.sh"
    script.write_text("#!/usr/bin/env bash\necho boom 1>&2\nexit 5\n")
    script.chmod(0o755)
    runner = ProcessRunner()

    with pytest.raises(CommandExecutionError) as excinfo:
        runner.run([str(script)], stream_output=True)

    assert excinfo.value.returncode == 5


def test_stream_output_missing_executable_raises_typed_error(tmp_path: Path) -> None:
    runner = ProcessRunner()

    with pytest.raises(ExecutableNotFoundError):
        runner.run([str(tmp_path / "does-not-exist")], stream_output=True)


def test_stream_output_timeout_raises_typed_error(tmp_path: Path) -> None:
    script = write_sleeping_executable(tmp_path / "slow", seconds=5)
    runner = ProcessRunner()

    with pytest.raises(CommandTimeoutError):
        runner.run([str(script)], timeout_seconds=0.05, stream_output=True)


def test_stream_output_kills_child_when_an_external_deadline_interrupts_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeadlineExpired(Exception):
        pass

    class Process:
        stdout = StringIO()
        stderr = StringIO()
        killed = False

        def poll(self):  # noqa: ANN201
            return None if not self.killed else -9

        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            if not self.killed:
                raise DeadlineExpired()
            return -9

        def kill(self) -> None:
            self.killed = True

    process = Process()
    monkeypatch.setattr("olf.tooling.process.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("olf.tooling.process.os.killpg", lambda *args: process.kill())

    with pytest.raises(DeadlineExpired):
        ProcessRunner().run(["long-running-command"], stream_output=True)

    assert process.killed


def test_cwd_is_observed_by_child(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    runner = ProcessRunner()

    result = runner.run([str(script)], cwd=work_dir)

    payload = json.loads(result.stdout)
    assert Path(payload["cwd"]).resolve() == work_dir.resolve()


def test_command_env_overlay_reaches_child_without_mutating_parent(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake", env_keys=["OLF_TEST_VAR"])
    runner = ProcessRunner()
    assert "OLF_TEST_VAR" not in os.environ

    result = runner.run([str(script)], env={"OLF_TEST_VAR": "scoped-value"})

    payload = json.loads(result.stdout)
    assert payload["env"]["OLF_TEST_VAR"] == "scoped-value"
    assert "OLF_TEST_VAR" not in os.environ


def test_shell_metacharacters_arrive_as_literal_argv(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake")
    runner = ProcessRunner()
    dangerous = "hello; rm -rf / && echo $HOME `whoami` * 'quoted'"

    result = runner.run([str(script), dangerous])

    payload = json.loads(result.stdout)
    assert payload["argv"] == [dangerous]


def test_input_text_reaches_child_and_is_not_recorded_on_result(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake")
    runner = ProcessRunner()

    result = runner.run([str(script)], input_text="super-secret-stdin")

    payload = json.loads(result.stdout)
    assert payload["stdin"] == "super-secret-stdin"
    # CommandResult itself never carries input_text, regardless of whether
    # the invoked tool happens to echo stdin back on stdout (as this fake
    # executable does purely to make the assertion above possible).
    assert "input_text" not in CommandResult.__dataclass_fields__


def test_stdin_path_streams_binary_content_to_child(tmp_path: Path) -> None:
    script = tmp_path / "reader.py"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys\ndata = sys.stdin.buffer.read()\nprint(len(data), data.hex())\n"
    )
    script.chmod(0o755)
    payload = tmp_path / "payload.bin"
    content = b"\x00\x01binary-content\xff\xfe"
    payload.write_bytes(content)
    runner = ProcessRunner()

    result = runner.run([str(script)], stdin_path=payload)

    assert result.stdout.strip() == f"{len(content)} {content.hex()}"


def test_stdin_path_and_input_text_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        Command(argv=("true",), input_text="a", stdin_path=Path("/tmp/x"))


def test_command_normalizes_path_like_argv_entries(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake")
    command = Command(argv=(script, "arg"))  # type: ignore[arg-type]

    assert command.argv == (str(script), "arg")


def test_empty_argv_is_rejected() -> None:
    with pytest.raises(ValueError):
        Command(argv=())


def test_retry_policy_runs_command_until_success(tmp_path: Path) -> None:
    marker = tmp_path / "attempts"
    script = tmp_path / "flaky.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"marker = {str(marker)!r}\n"
        "attempts = int(open(marker).read()) if __import__('os').path.exists(marker) else 0\n"
        "attempts += 1\n"
        "open(marker, 'w').write(str(attempts))\n"
        "sys.exit(0 if attempts >= 2 else 1)\n"
    )
    script.chmod(0o755)
    runner = ProcessRunner()
    sleeps: list[float] = []

    result = runner.run(
        [str(script)],
        retry_policy=RetryPolicy(max_attempts=4, delay_seconds=0),
    )

    assert result.returncode == 0
    assert int(marker.read_text()) == 2
    assert sleeps == []  # delay_seconds=0, nothing meaningful to assert beyond no crash
