"""Every common command runs with no cluster, cloud or platform credential (#536, ADR 0020).

Each command visible on the common surface is run with `KUBECONFIG` pointing at
nothing, no `AWS_*` or `AZURE_*` variable, and an empty `OLF_HOME`, so there is
no platform session either. Child processes are recorded and answered as if
they succeeded, and every socket connection is refused, so nothing is built,
downloaded or deployed; a command may still fail on its own inputs. What it may
not do is start a cluster or cloud tool, or try to reach a host at all, except
`toolchain install`, whose job is to download public releases.
"""

from __future__ import annotations

import inspect
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

from olf.cli import app
from olf.common.initialization import render_profile
from olf.common.profile import Preset, StageName
from olf.common.tooling import resolver

REPO_ROOT = Path(__file__).resolve().parents[4]
CLUSTER_OR_CLOUD = {"kubectl", "helm", "kind", "aws", "az", "gcloud", "eksctl", "kubelogin"}
PROFILE = "openlakeforge.yaml"
# Commands whose job is to fetch public release artefacts, which the common row of ADR 0020 allows; the guard
# refuses the download, and they fail on that, never on a credential.
FETCHES_PUBLIC_RELEASES = {("toolchain", "install")}


def _common_paths() -> list[tuple[str, ...]]:
    def leaves(group: TyperGroup, path: tuple[str, ...] = (), hidden: bool = False) -> Iterator[Any]:
        for name, command in group.commands.items():
            if isinstance(command, TyperGroup):
                yield from leaves(command, (*path, name), hidden or command.hidden)
            else:
                yield (*path, name), command.callback, hidden or command.hidden

    return [
        path
        for path, callback, hidden in leaves(typer.main.get_group(app))
        if not hidden
        and path[0] != "infra"
        and not inspect.unwrap(callback).__module__.startswith(("olf.platform", "olf.infra"))
    ]


COMMON_PATHS = _common_paths()

# Arguments that get each command past its own option parsing; `{project}` is a project `olf init` scaffolded.
ARGUMENTS: dict[tuple[str, ...], list[str]] = {
    ("layers", "enabled"): ["--layer", "analytics", "-f", "{project}/" + PROFILE],
    ("release", "checksums"): ["--dir", "{project}"],
    ("release", "workflow", "require-green-main"): ["--repo", "openlakeforge/openlakeforge", "--sha", "0" * 40],
    ("source", "new"): ["crm_extra", "--resource", "accounts", "--project", "{project}"],
    ("domain", "new"): ["hr", "--input", "crm/accounts", "--project", "{project}"],
    ("product", "new"): ["hr/headcount", "--gold-table", "mart_headcount", "--project", "{project}"],
    ("profile", "validate"): ["--project", "{project}"],
    ("profile", "resolve"): ["--project", "{project}"],
    ("project", "validate"): ["--project", "{project}"],
    ("project", "graph"): ["--project", "{project}"],
    ("images", "build"): ["project-code"],
}


class Guard:
    """What the commands tried: the executables they started and the hosts they connected to."""

    def __init__(self) -> None:
        self.executables: list[str] = []
        self.connections: list[Any] = []

    def run(self, args: Any, *_: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(arg) for arg in args] if isinstance(args, (list, tuple)) else str(args).split()
        self.executables.append(Path(argv[0]).name)
        empty: Any = "" if kwargs.get("text") or kwargs.get("encoding") else b""
        return subprocess.CompletedProcess(argv, 0, empty, empty)

    def popen(self, args: Any, *_: Any, **__: Any) -> Any:
        self.run(args)
        raise OSError("the test answers no streaming child process")

    def connect(self, _socket: socket.socket, address: Any) -> None:
        self.connections.append(address)
        raise OSError("the test refuses every connection")


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Guard:
    for name in list(os.environ):
        if name.startswith(("AWS_", "AZURE_", "ARM_", "GOOGLE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KUBECONFIG", "/nonexistent")
    monkeypatch.setenv("OLF_HOME", str(tmp_path / "olf-home"))
    found = Guard()
    monkeypatch.setattr(subprocess, "run", found.run)
    monkeypatch.setattr(subprocess, "Popen", found.popen)
    # A plain function, so `socket.connect(address)` binds the socket as its first argument.
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: found.connect(sock, address))
    # No managed toolchain is installed under an empty OLF_HOME; a tool resolves to its name and is never run.
    for kind in (resolver.ManagedExecutableResolver, resolver.PathExecutableResolver):
        monkeypatch.setattr(kind, "resolve", lambda self, tool: Path(tool))
    return found


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: Guard) -> Path:
    """The demo project `olf init` copies, with its profile, built without running init's Docker check."""
    root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "lakehouse_code", root / "lakehouse_code", ignore=shutil.ignore_patterns("__pycache__"))
    profile = render_profile(name="project", stages=(StageName.DEV,), preset=Preset.SLIM)
    (root / PROFILE).write_text(profile, encoding="utf-8")
    # `olf init` writes into this root; it must be empty, and never the checkout.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("OPENLAKEFORGE_PROJECT_ROOT", str(fresh))
    # A checkout's own files for the repository checks, run from the checkout as CI runs them.
    monkeypatch.chdir(REPO_ROOT)
    return root


@pytest.mark.parametrize("path", COMMON_PATHS, ids=" ".join)
def test_a_common_command_starts_no_cluster_or_cloud_tool_and_reaches_no_host(
    path: tuple[str, ...], project: Path, guard: Guard
) -> None:
    args = [arg.replace("{project}", str(project)) for arg in ARGUMENTS.get(path, [])]
    guard.executables.clear()

    result = CliRunner().invoke(app, [*path, *args])

    assert not isinstance(result.exception, AssertionError), result.output
    assert CLUSTER_OR_CLOUD.isdisjoint(guard.executables), (guard.executables, result.output)
    if path not in FETCHES_PUBLIC_RELEASES:
        assert guard.connections == [], result.output


def test_the_list_covers_the_common_surface() -> None:
    assert ("init",) in COMMON_PATHS and ("check", "all") in COMMON_PATHS and ("project", "validate") in COMMON_PATHS
    assert not any(path[0] in {"login", "whoami", "logout", "query", "jobs", "runs"} for path in COMMON_PATHS)


def test_the_guard_sees_a_cluster_tool_and_a_connection(guard: Guard) -> None:
    subprocess.run(["/usr/local/bin/kubectl", "get", "pods"], check=False)
    with pytest.raises(OSError), socket.create_connection(("192.0.2.1", 443), timeout=1):
        pass

    assert guard.executables == ["kubectl"] and guard.connections == [("192.0.2.1", 443)]
