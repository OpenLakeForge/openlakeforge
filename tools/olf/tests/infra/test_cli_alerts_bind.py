"""`olf infra alerts bind|bindings` write a receiver's webhook URL without ever exposing it (#1425).

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
from typer.testing import CliRunner

from olf.cli import app
from olf.common.profile import AlertReceiver, SecretRef
from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import CommandResult, ProcessRunner
from olf.infra.commands import alerts as alerts_cmd

runner = CliRunner()
URL = "https://hooks.example.com/services/T000/B000/s3cr3t-token"
OTHER_URL = "https://hooks.example.com/services/T111/B111/other-s3cr3t"


class FakeCluster(ProcessRunner):
    """kubectl against an in-memory cluster: get namespace|secret, and apply (server-side, stdin)."""

    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, command: Any, **kwargs: Any) -> CommandResult:  # type: ignore[override]
        argv = [str(part) for part in command]
        stdin = kwargs.get("input_text")
        self.calls.append((argv, stdin))
        verb = next(part for part in argv[1:] if part in ("get", "apply"))
        stdout = stderr = ""
        code = 0
        if verb == "apply":
            manifest = json.loads(stdin or "")
            self.objects[(manifest["kind"].lower(), manifest["metadata"]["name"])] = manifest
        else:
            kind, name = argv[argv.index("get") + 1], argv[argv.index("get") + 2]
            if (kind, name) in self.objects:
                output = argv[argv.index("-o") + 1] if "-o" in argv else ""
                if output.startswith("go-template="):
                    stdout = "\n".join(self.objects[(kind, name)].get("data", {}))
                else:
                    stdout = json.dumps(self.objects[(kind, name)])
            else:
                code, stderr = 1, f'Error from server (NotFound): "{name}" not found'
        return CommandResult(argv=tuple(argv), returncode=code, stdout=stdout, stderr=stderr, duration_seconds=0.0)

    @property
    def applies(self) -> list[tuple[list[str], str | None]]:
        return [call for call in self.calls if "apply" in call[0]]


class FakeResolver:
    def resolve(self, _name: str) -> Path:
        return Path("kubectl")


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    fake = FakeCluster()
    receivers = (
        AlertReceiver("on-call", "webhook", SecretRef("alert-webhooks", "on-call")),
        AlertReceiver("ops", "webhook", SecretRef("alert-webhooks", "ops")),
        AlertReceiver("solo", "webhook", SecretRef("solo-hook", "url")),
    )
    provider = SimpleNamespace(
        tools=SimpleNamespace(kubectl=Kubectl(fake, FakeResolver())), env={"KUBE_CONTEXT": "kind-x"}
    )
    context = SimpleNamespace(
        shared_namespace="olf-system",
        kube_context="kind-y",
        paths=SimpleNamespace(kubeconfig_path=None),
        topology=SimpleNamespace(observability=SimpleNamespace(alert_receivers=receivers)),
    )
    monkeypatch.setattr("olf.infra.commands._shared.deployment_context_for_profile", lambda *_a, **_k: context)
    monkeypatch.setattr("olf.infra.commands.project._profile_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(alerts_cmd, "_stdin_is_tty", lambda: False)
    monkeypatch.setenv("HOOK_URL", URL)
    monkeypatch.setenv("OTHER_URL", OTHER_URL)
    return fake


def _bind(receiver: str = "on-call", *args: str) -> Any:
    return runner.invoke(app, ["infra", "alerts", "bind", receiver, "-f", "openlakeforge.yaml", *args])


def _bindings() -> Any:
    return runner.invoke(app, ["infra", "alerts", "bindings", "-f", "openlakeforge.yaml"])


def _decoded(cluster: FakeCluster, name: str = "alert-webhooks") -> dict[str, str]:
    return {key: base64.b64decode(v).decode() for key, v in cluster.objects[("secret", name)]["data"].items()}


def test_bind_creates_the_secret_in_the_shared_namespace_and_reports_names_only(cluster: FakeCluster) -> None:
    result = _bind("on-call", "--from-env", "HOOK_URL")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["receiver on-call: secret alert-webhooks key on-call in olf-system (created)"]
    secret = cluster.objects[("secret", "alert-webhooks")]
    assert _decoded(cluster) == {"on-call": URL}
    assert secret["type"] == "Opaque"
    assert secret["metadata"]["namespace"] == "olf-system"
    assert secret["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "olf"}


def test_a_missing_namespace_is_created_before_the_secret(cluster: FakeCluster) -> None:
    _bind("on-call", "--from-env", "HOOK_URL")

    assert ("namespace", "olf-system") in cluster.objects
    kinds = [json.loads(stdin or "")["kind"] for _argv, stdin in cluster.applies]
    assert kinds == ["Namespace", "Secret"]


def test_the_url_reaches_kubectl_on_stdin_only(cluster: FakeCluster) -> None:
    result = _bind("on-call", "--from-env", "HOOK_URL")

    assert URL not in result.output
    for argv, _stdin in cluster.calls:
        assert not any(URL in part for part in argv)
    assert all("--server-side" in argv for argv, _ in cluster.applies)


def test_rebinding_the_same_value_reports_unchanged_and_writes_nothing(cluster: FakeCluster) -> None:
    _bind("on-call", "--from-env", "HOOK_URL")
    before = len(cluster.applies)
    result = _bind("on-call", "--from-env", "HOOK_URL")

    assert result.exit_code == 0
    assert "(unchanged)" in result.output
    assert len(cluster.applies) == before


def test_a_new_value_updates_the_key(cluster: FakeCluster) -> None:
    _bind("on-call", "--from-env", "HOOK_URL")
    result = _bind("on-call", "--from-env", "OTHER_URL")

    assert "(updated)" in result.output
    assert _decoded(cluster) == {"on-call": OTHER_URL}


def test_binding_a_second_receiver_keeps_the_first_key(cluster: FakeCluster) -> None:
    _bind("on-call", "--from-env", "HOOK_URL")
    result = _bind("ops", "--from-env", "OTHER_URL")

    assert result.exit_code == 0, result.output
    assert "(updated)" in result.output
    assert _decoded(cluster) == {"on-call": URL, "ops": OTHER_URL}


def test_from_file_drops_one_trailing_newline(cluster: FakeCluster, tmp_path: Path) -> None:
    path = tmp_path / "url"
    path.write_text(URL + "\n")

    result = _bind("solo", "--from-file", str(path))

    assert result.exit_code == 0, result.output
    assert _decoded(cluster, "solo-hook") == {"url": URL}


def test_without_a_source_a_terminal_prompts_and_no_terminal_fails(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = _bind("on-call")
    assert refused.exit_code == 1
    assert "--from-env" in refused.output and "--from-file" in refused.output
    assert cluster.applies == []

    monkeypatch.setattr(alerts_cmd, "_stdin_is_tty", lambda: True)
    prompted = runner.invoke(app, ["infra", "alerts", "bind", "on-call", "-f", "openlakeforge.yaml"], input=URL + "\n")
    assert prompted.exit_code == 0, prompted.output
    assert URL not in prompted.output
    assert _decoded(cluster) == {"on-call": URL}


def test_a_non_https_value_fails_before_any_write_and_is_not_echoed(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAD_URL", "http://hooks.example.com/s3cr3t-plain")

    result = _bind("on-call", "--from-env", "BAD_URL")

    assert result.exit_code == 1
    assert "must use https" in result.output
    assert "s3cr3t-plain" not in result.output
    assert cluster.applies == []


def test_dry_run_writes_nothing_and_says_what_would_happen(cluster: FakeCluster) -> None:
    created = _bind("on-call", "--from-env", "HOOK_URL", "--dry-run")
    assert created.stdout.splitlines() == [
        "receiver on-call: secret alert-webhooks key on-call in olf-system (would create)"
    ]
    assert cluster.applies == []

    _bind("on-call", "--from-env", "HOOK_URL")
    before = len(cluster.applies)
    updated = _bind("on-call", "--from-env", "OTHER_URL", "--dry-run")
    assert "(would update)" in updated.output
    assert len(cluster.applies) == before
    assert _decoded(cluster) == {"on-call": URL}


def test_an_unknown_receiver_lists_the_declared_ones(cluster: FakeCluster) -> None:
    result = _bind("nope", "--from-env", "HOOK_URL")

    assert result.exit_code == 1
    assert "unknown alert receiver 'nope'" in result.output
    assert "on-call, ops, solo" in result.output
    assert cluster.applies == []


def test_both_value_options_are_refused(cluster: FakeCluster, tmp_path: Path) -> None:
    result = _bind("on-call", "--from-env", "HOOK_URL", "--from-file", str(tmp_path / "x"))

    assert result.exit_code == 1
    assert cluster.applies == []


def test_admission_failure_does_not_echo_the_url(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.common.deployment.errors import CommandExecutionError

    original = cluster.run

    def reject(command: Any, **kwargs: Any) -> CommandResult:
        if "apply" in command and '"kind": "Secret"' in kwargs.get("input_text", ""):
            raise CommandExecutionError(
                command, 1, stderr=f"admission rejected: {URL} {base64.b64encode(URL.encode()).decode()}"
            )
        return original(command, **kwargs)

    monkeypatch.setattr(cluster, "run", reject)
    result = _bind("on-call", "--from-env", "HOOK_URL")

    assert result.exit_code == 1
    assert "could not apply secret alert-webhooks" in result.output
    assert URL not in result.output
    assert base64.b64encode(URL.encode()).decode() not in result.output


def test_bindings_reports_missing_secret_missing_key_and_bound(cluster: FakeCluster) -> None:
    absent = _bindings()
    assert absent.exit_code == 1
    assert "receiver on-call: secret alert-webhooks key on-call in olf-system: Secret missing" in absent.output
    assert "receiver solo: secret solo-hook key url in olf-system: Secret missing" in absent.output

    _bind("on-call", "--from-env", "HOOK_URL")
    partial = _bindings()
    assert partial.exit_code == 1
    assert "receiver on-call: secret alert-webhooks key on-call in olf-system: bound" in partial.output
    assert "key missing (the Secret has: on-call)" in partial.output
    assert URL not in partial.output

    _bind("ops", "--from-env", "OTHER_URL")
    _bind("solo", "--from-env", "HOOK_URL")
    complete = _bindings()
    assert complete.exit_code == 0, complete.output
    assert complete.output.count(": bound") == 3
