from __future__ import annotations

import json
from pathlib import Path

import pytest
from _tooling_support import write_fake_executable

from olf.deployment.errors import CommandExecutionError
from olf.tooling.process import ProcessRunner
from olf.tooling.redact import is_sensitive_env_name, redact_argv, redact_env

SENSITIVE_NAMES = [
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AZURE_CLIENT_SECRET",
    "OPENLINEAGE_API_KEY",
    "DB_PASSWORD",
    "DOCKER_PASSWD",
    "GITHUB_TOKEN",
    "PRIVATE_KEY_PATH",
    "SOME_CREDENTIAL_FILE",
    "BASIC_AUTH_HEADER",
]


@pytest.mark.parametrize("name", SENSITIVE_NAMES)
def test_sensitive_env_names_are_detected(name: str) -> None:
    assert is_sensitive_env_name(name)


@pytest.mark.parametrize("name", ["NAMESPACE", "KUBE_CONTEXT", "PATH", "HOME", "DEPLOYMENT_SCOPE"])
def test_non_sensitive_env_names_are_not_flagged(name: str) -> None:
    assert not is_sensitive_env_name(name)


def test_redact_env_replaces_only_sensitive_values() -> None:
    env = {"AWS_SECRET_ACCESS_KEY": "very-secret", "NAMESPACE": "lakehouse"}

    redacted = redact_env(env)

    assert redacted["AWS_SECRET_ACCESS_KEY"] == "<redacted>"
    assert redacted["NAMESPACE"] == "lakehouse"
    assert "very-secret" not in redacted.values()


@pytest.mark.parametrize(
    "argv",
    [
        ["az", "login", "--service-principal", "-u", "client", "-p", "super-secret"],
        ["docker", "login", "--username", "foo", "--password", "abc"],
        ["some-cli", "--token=super-secret"],
        ["some-cli", "--client-secret", "super-secret"],
        ["some-cli", "--api-key", "super-secret"],
    ],
)
def test_redact_argv_hides_secret_values_following_known_flags(argv: list[str]) -> None:
    redacted = redact_argv(argv)

    joined = " ".join(redacted)
    assert "super-secret" not in joined
    assert "abc" not in joined
    assert "<redacted>" in joined


def test_redact_argv_preserves_non_secret_tokens() -> None:
    argv = ["kubectl", "--context", "kind-openlakeforge-local", "get", "pods", "-n", "lakehouse"]

    assert redact_argv(argv) == argv


def test_command_execution_error_does_not_leak_env_secrets_in_str() -> None:
    error = CommandExecutionError(
        ("az", "login"),
        1,
        stderr="",
        env={"AZURE_CLIENT_SECRET": "very-secret", "NAMESPACE": "lakehouse"},
    )

    text = str(error)
    assert "very-secret" not in text
    assert "<redacted>" in text
    assert "lakehouse" in text


def test_command_execution_error_does_not_leak_argv_secrets_in_str() -> None:
    error = CommandExecutionError(
        ("az", "login", "--service-principal", "-u", "client", "-p", "super-secret"),
        1,
    )

    text = str(error)
    assert "super-secret" not in text
    assert "<redacted>" in text


def test_process_runner_raises_error_without_leaking_secret_argv(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake", exit_code=1)
    runner = ProcessRunner()

    with pytest.raises(CommandExecutionError) as excinfo:
        runner.run([str(script), "--password", "super-secret"])

    assert "super-secret" not in str(excinfo.value)


def test_process_runner_raises_error_without_leaking_secret_env(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake", exit_code=1)
    runner = ProcessRunner()

    with pytest.raises(CommandExecutionError) as excinfo:
        runner.run([str(script)], env={"OPENLINEAGE_API_KEY": "very-secret"})

    assert "very-secret" not in str(excinfo.value)


def test_original_argv_still_reaches_subprocess_despite_diagnostic_redaction(tmp_path: Path) -> None:
    script = write_fake_executable(tmp_path / "fake")
    runner = ProcessRunner()

    result = runner.run([str(script), "--password", "super-secret"])

    payload = json.loads(result.stdout)
    assert payload["argv"] == ["--password", "super-secret"]
