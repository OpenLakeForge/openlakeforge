from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from _tooling_support import RecordingRunner

from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver
from olf.infra import break_glass


def _cluster(stdout: str = "alice\n") -> tuple[break_glass.Cluster, RecordingRunner]:
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0))
    kubectl = Kubectl(runner, PathExecutableResolver(overrides={"kubectl": Path("kubectl")}))
    return break_glass.Cluster(kubectl, "kind-x", Path("/kc"), "olf-system", "olf-dev"), runner


def _verbs(runner: RecordingRunner) -> list[str]:
    return [next(a for a in call.argv if a in ("config", "create", "patch", "get", "delete")) for call in runner.calls]


def test_use_is_recorded_before_access_and_completed_after() -> None:
    cluster, runner = _cluster()
    before_access: list[str] = []

    def run(argv: list[str], **_: Any) -> None:
        before_access.extend(_verbs(runner))

    name = break_glass.open_session(cluster, "dex", "idp down", 5, run=run)

    assert before_access == ["config", "create"]
    create = next(call for call in runner.calls if "create" in call.argv)
    record = json.loads(create.kwargs["input_text"])
    assert record["metadata"]["name"] == name
    assert record["metadata"]["labels"] == {break_glass.LABEL: "true"}
    data = record["data"]
    assert (data["service"], data["reason"], data["user"], data["ttl_minutes"]) == ("dex", "idp down", "alice", "5")
    done = json.loads(runner.last_call.argv[runner.last_call.argv.index("-p") + 1])
    assert done["data"]["ended"] and int(done["data"]["duration_seconds"]) >= 0
    assert "delete" not in _verbs(runner)


def test_ttl_expiry_still_completes_the_record() -> None:
    cluster, runner = _cluster()

    def run(argv: list[str], **kwargs: Any) -> None:
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(argv, 60)

    break_glass.open_session(cluster, "trino", "r", 1, run=run)

    assert _verbs(runner)[-1] == "patch"


def test_pod_cli_services_open_a_shell_in_their_namespace() -> None:
    cluster, _ = _cluster()
    argvs: list[list[str]] = []
    break_glass.open_session(cluster, "superset", "r", 1, run=lambda argv, **_: argvs.append(argv))
    assert argvs[0][-5:] == ["deploy/superset", "-n", "olf-dev", "--", "sh"]


def test_render_flags_a_record_without_an_end() -> None:
    text = break_glass.render(
        [
            {"started": "t1", "user": "a", "service": "dex", "reason": "x"},
            {"started": "t0", "user": "b", "service": "trino", "reason": "y", "ended": "t", "duration_seconds": "9"},
        ]
    ).splitlines()
    assert "unclean exit" in text[0]
    assert "9s" in text[1]
    assert break_glass.render([]) == "none"
    assert break_glass.render(None) == "unavailable"


def test_recent_orders_newest_first() -> None:
    items = [{"data": {"started": "2026-01-01"}}, {"data": {"started": "2026-02-01"}}]
    cluster, _ = _cluster(json.dumps({"items": items}))
    assert break_glass.recent(cluster.kubectl, "olf-system", limit=1) == [{"started": "2026-02-01"}]


@pytest.mark.parametrize("service", list(break_glass.TARGETS))
def test_every_target_builds_an_argv(service: str) -> None:
    cluster, _ = _cluster()
    argvs: list[list[str]] = []
    break_glass.open_session(cluster, service, "r", 1, run=lambda argv, **_: argvs.append(argv))
    assert "--context" in argvs[0]


def test_trino_is_forwarded_on_its_http_port() -> None:
    cluster, _ = _cluster()
    argvs: list[list[str]] = []
    break_glass.open_session(cluster, "trino", "r", 1, run=lambda argv, **_: argvs.append(argv))
    assert "8080:8080" in argvs[0]


def test_default_attach_inherits_stdin_and_ignores_the_exit_status(tmp_path: Path) -> None:
    out = tmp_path / "out"
    script = tmp_path / "fake"
    script.write_text(f'#!/bin/sh\nread line\necho "$line $OLF_BG" > {out}\nexit 3\n')
    script.chmod(0o755)
    read_end, write_end = os.pipe()
    os.write(write_end, b"typed\n")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        break_glass._attach([str(script)], timeout=10, env={"OLF_BG": "set"})
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)

    assert out.read_text().strip() == "typed set"


def test_default_attach_kills_the_child_when_the_ttl_passes(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = tmp_path / "fake"
    script.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 60\n")
    script.chmod(0o755)

    with pytest.raises(subprocess.TimeoutExpired):
        break_glass._attach([str(script)], timeout=1, env={})

    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
