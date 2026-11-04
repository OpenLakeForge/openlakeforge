"""`olf infra source bind|unbind` write a stage's source bindings without ever exposing a secret value (#760).

Only the cluster is faked: a runner standing in for `kubectl` that keeps the objects it is applied. The real
`Kubectl` adapter builds the argv, so what the tests inspect is what a process list would show.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openlakeforge_core.inventory import Source
from typer.testing import CliRunner

from olf.cli import app
from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import CommandResult, ProcessRunner
from olf.infra.commands import source as source_cmd

runner = CliRunner()
PASSWORD = "hunter2-s3cret"
TOKEN = "tok-4567-xyz"


class FakeCluster(ProcessRunner):
    """kubectl against an in-memory namespace: get, apply (server-side, stdin) and delete."""

    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, command: Any, **kwargs: Any) -> CommandResult:  # type: ignore[override]
        argv = [str(part) for part in command]
        stdin = kwargs.get("input_text")
        self.calls.append((argv, stdin))
        verb = next(part for part in argv[1:] if part in ("get", "apply", "delete"))
        stdout = stderr = ""
        code = 0
        if verb == "apply":
            manifest = json.loads(stdin or "")
            self.objects[manifest["metadata"]["name"]] = manifest
        elif verb == "get":
            name = argv[argv.index("get") + 2]
            if name in self.objects:
                stdout = json.dumps(self.objects[name])
            else:
                code, stderr = 1, f'Error from server (NotFound): "{name}" not found'
        else:
            name = argv[argv.index("delete") + 2]
            if self.objects.pop(name, None) is not None:
                stdout = f'"{name}" deleted'
        return CommandResult(argv=tuple(argv), returncode=code, stdout=stdout, stderr=stderr, duration_seconds=0.0)

    @property
    def applies(self) -> list[tuple[list[str], str | None]]:
        return [call for call in self.calls if "apply" in call[0]]


class FakeResolver:
    def resolve(self, _name: str) -> Path:
        return Path("kubectl")


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCluster:
    fake = FakeCluster()
    source = Source(
        descriptor_path=tmp_path / "source.yaml",
        name="open_food",
        display_name="Open Food",
        resources=(),
        runtime_config=("base_url", "country"),
        runtime_secrets=("password", "token"),
    )
    plain = Source(descriptor_path=tmp_path / "plain.yaml", name="plain", display_name="Plain", resources=())
    provider = SimpleNamespace(
        tools=SimpleNamespace(kubectl=Kubectl(fake, FakeResolver())), env={"KUBE_CONTEXT": "kind-x"}
    )  # type: ignore[arg-type]
    context = SimpleNamespace(namespace="olf-dev", kube_context="kind-y", paths=SimpleNamespace(kubeconfig_path=None))
    monkeypatch.setattr("olf.infra.commands._shared.deployment_context_for_profile", lambda *_a, **_k: context)
    monkeypatch.setattr("olf.infra.commands.project._profile_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(
        "openlakeforge_core.load_lakehouse_inventory", lambda _path: SimpleNamespace(sources=[source, plain])
    )
    monkeypatch.setattr(source_cmd, "_stdin_is_tty", lambda: False)
    monkeypatch.setenv("OFF_PASSWORD", PASSWORD)
    return fake


def _bind(*args: str) -> Any:
    base = ["infra", "source", "bind", "open_food", "-f", "openlakeforge.yaml", "--stage", "dev"]
    full = ["--set", "base_url=https://x.example", "--set", "country=be"]
    full += ["--secret-from-env", "password=OFF_PASSWORD", "--secret-from-env", "token=OFF_TOKEN"]
    return runner.invoke(app, [*base, *full, *args])


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFF_TOKEN", TOKEN)


def _decoded(manifest: dict[str, Any]) -> dict[str, str]:
    return {key: base64.b64decode(value).decode() for key, value in manifest["data"].items()}


def test_bind_creates_both_objects_and_reports_key_names_only(cluster: FakeCluster) -> None:
    result = _bind()

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "configmap olf-source-open-food: base_url, country (created)",
        "secret olf-source-open-food-credentials: password, token (created)",
    ]
    config, secret = cluster.objects["olf-source-open-food"], cluster.objects["olf-source-open-food-credentials"]
    assert config["data"] == {"base_url": "https://x.example", "country": "be"}
    assert _decoded(secret) == {"password": PASSWORD, "token": TOKEN}
    assert secret["type"] == "Opaque"
    for obj in (config, secret):
        assert obj["metadata"]["namespace"] == "olf-dev"
        assert obj["metadata"]["labels"] == {
            "app.kubernetes.io/managed-by": "olf",
            "openlakeforge.io/source": "open_food",
        }


def test_secret_values_reach_kubectl_on_stdin_only(cluster: FakeCluster) -> None:
    result = _bind()

    assert PASSWORD not in result.output and TOKEN not in result.output
    assert result.stderr == "" or PASSWORD not in result.stderr
    for argv, _stdin in cluster.calls:
        assert not any(secret in part for part in argv for secret in (PASSWORD, TOKEN))
        assert not any(
            base64.b64encode(secret.encode()).decode() in part for part in argv for secret in (PASSWORD, TOKEN)
        )
    assert len(cluster.applies) == 2
    for argv, stdin in cluster.applies:
        assert argv[-5:] == ["apply", "--server-side", "--force-conflicts", "-f", "-"]
        assert stdin is not None
    assert PASSWORD in "".join(
        _decoded(json.loads(stdin or "{}")).get("password", "") for _a, stdin in cluster.applies[1:]
    )


def test_secret_from_file_strips_one_trailing_newline(cluster: FakeCluster, tmp_path: Path) -> None:
    token = tmp_path / "token.txt"
    token.write_text("from-file\n")

    result = runner.invoke(
        app,
        ["infra", "source", "bind", "open_food", "-f", "p.yaml", "--set", "base_url=u", "--set", "country=c"]
        + ["--secret-from-env", "password=OFF_PASSWORD", "--secret-from-file", f"token={token}"],
    )

    assert result.exit_code == 0, result.output
    assert _decoded(cluster.objects["olf-source-open-food-credentials"])["token"] == "from-file"


def test_rebinding_the_same_values_is_unchanged_and_changed_values_are_updated(cluster: FakeCluster) -> None:
    _bind()
    again = _bind()
    assert again.stdout.count("(unchanged)") == 2
    assert len(cluster.applies) == 2  # the re-run wrote nothing

    changed = runner.invoke(
        app,
        ["infra", "source", "bind", "open_food", "-f", "p.yaml", "--set", "base_url=https://y", "--set", "country=be"]
        + ["--secret-from-env", "password=OFF_PASSWORD", "--secret-from-env", "token=OFF_TOKEN"],
    )
    assert changed.stdout.splitlines()[0] == "configmap olf-source-open-food: base_url, country (updated)"
    assert changed.stdout.splitlines()[1].endswith("(unchanged)")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--set", "bogus=1"], "declares no key 'bogus' (config: base_url, country; secrets: password, token)"),
        (["--set", "password=leaked-value"], "'password' is a secret of source"),
        (["--secret-from-env", "country=OFF_TOKEN"], "'country' is a config key of source"),
    ],
)
def test_keys_are_validated_against_the_declared_runtime_block(
    cluster: FakeCluster, args: list[str], message: str
) -> None:
    result = runner.invoke(app, ["infra", "source", "bind", "open_food", "-f", "p.yaml", *args])

    assert result.exit_code == 1
    assert message in result.output
    assert "leaked-value" not in result.output
    assert cluster.calls == []


def test_a_declared_key_not_given_is_an_error_unless_partial(cluster: FakeCluster) -> None:
    result = runner.invoke(app, ["infra", "source", "bind", "open_food", "-f", "p.yaml", "--set", "base_url=u"])

    assert result.exit_code == 1
    assert "needs country, password, token" in result.output
    assert cluster.calls == []


def test_partial_keeps_the_keys_already_bound(cluster: FakeCluster) -> None:
    _bind()

    result = runner.invoke(
        app,
        ["infra", "source", "bind", "open_food", "-f", "p.yaml", "--partial", "--set", "country=nl"]
        + ["--secret-from-env", "token=OFF_TOKEN"],
    )

    assert result.exit_code == 0, result.output
    assert cluster.objects["olf-source-open-food"]["data"] == {"base_url": "https://x.example", "country": "nl"}
    assert _decoded(cluster.objects["olf-source-open-food-credentials"]) == {"password": PASSWORD, "token": TOKEN}


def test_dry_run_prints_the_plan_by_key_name_and_writes_nothing(cluster: FakeCluster) -> None:
    result = _bind("--dry-run")

    assert result.stdout.splitlines() == [
        "configmap olf-source-open-food: base_url, country (would create)",
        "secret olf-source-open-food-credentials: password, token (would create)",
    ]
    assert cluster.applies == [] and cluster.objects == {}
    assert PASSWORD not in result.output


def test_a_missing_secret_is_prompted_for_without_echo_when_stdin_is_a_terminal(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_cmd, "_stdin_is_tty", lambda: True)
    prompted: list[bool] = []

    def prompt(_text: str, *, hide_input: bool) -> str:
        prompted.append(hide_input)
        return "typed-secret"

    monkeypatch.setattr(source_cmd.typer, "prompt", prompt)
    result = runner.invoke(
        app,
        ["infra", "source", "bind", "open_food", "-f", "p.yaml", "--set", "base_url=u", "--set", "country=c"]
        + ["--secret-from-env", "password=OFF_PASSWORD"],
    )

    assert result.exit_code == 0, result.output
    assert prompted == [True]
    assert _decoded(cluster.objects["olf-source-open-food-credentials"])["token"] == "typed-secret"
    assert "typed-secret" not in result.output


def test_a_source_without_a_runtime_block_needs_no_binding(cluster: FakeCluster) -> None:
    result = runner.invoke(app, ["infra", "source", "bind", "plain", "-f", "p.yaml"])

    assert result.exit_code == 1
    assert "needs no binding" in result.output


def test_an_unknown_source_names_the_ones_the_project_has(cluster: FakeCluster) -> None:
    result = runner.invoke(app, ["infra", "source", "bind", "nope", "-f", "p.yaml"])

    assert result.exit_code == 1
    assert "this project has: open_food, plain" in result.output


def test_a_malformed_pair_is_not_echoed(cluster: FakeCluster) -> None:
    result = runner.invoke(app, ["infra", "source", "bind", "open_food", "-f", "p.yaml", "--set", "oops-secret-value"])

    assert result.exit_code == 1
    assert "--set expects KEY=VALUE" in result.output
    assert "oops-secret-value" not in result.output


def test_unbind_deletes_both_objects_and_is_repeatable(cluster: FakeCluster) -> None:
    _bind()
    args = ["infra", "source", "unbind", "open_food", "-f", "p.yaml", "--stage", "dev"]

    first, second = runner.invoke(app, args), runner.invoke(app, args)

    assert first.stdout.splitlines() == [
        "configmap olf-source-open-food: deleted",
        "secret olf-source-open-food-credentials: deleted",
    ]
    assert second.stdout.count("absent") == 2
    assert cluster.objects == {}


def test_unbind_cleans_up_objects_after_runtime_is_removed(cluster: FakeCluster) -> None:
    cluster.objects["olf-source-plain"] = {"data": {"retired_url": "old"}}
    cluster.objects["olf-source-plain-credentials"] = {"data": {"retired_token": TOKEN}}

    result = runner.invoke(app, ["infra", "source", "unbind", "plain", "-f", "p.yaml"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "configmap olf-source-plain: deleted",
        "secret olf-source-plain-credentials: deleted",
    ]
    assert cluster.objects == {}


def test_admission_failure_does_not_echo_secret_values(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.common.deployment.errors import CommandExecutionError

    original = cluster.run

    def reject(command: Any, **kwargs: Any) -> CommandResult:
        if "apply" in command and '"kind": "Secret"' in kwargs.get("input_text", ""):
            raise CommandExecutionError(
                command, 1, stderr=f"admission rejected: {PASSWORD} {base64.b64encode(PASSWORD.encode()).decode()}"
            )
        return original(command, **kwargs)

    monkeypatch.setattr(cluster, "run", reject)
    result = _bind()

    assert result.exit_code == 1
    assert "could not apply secret olf-source-open-food-credentials" in result.output
    assert PASSWORD not in result.output
    assert base64.b64encode(PASSWORD.encode()).decode() not in result.output
