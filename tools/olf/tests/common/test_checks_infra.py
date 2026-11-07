from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from olf.common.commands.checks import _infra
from olf.common.tooling.helm import Helm
from olf.common.tooling.process import CommandResult, ProcessRunner

# Stands in for `helm pull`: fails on the first call, then writes the tarball named like helm does.
_FAKE_HELM = """#!/bin/sh
count="$(dirname "$0")/calls"
echo x >> "$count"
[ "$(wc -l < "$count")" -eq 1 ] && exit 1
while [ $# -gt 0 ]; do [ "$1" = --destination ] && dest="$2"; shift; done
touch "$dest/cert-manager-v1.21.2.tgz"
"""


@pytest.fixture
def fake_helm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Helm:
    script = tmp_path / "bin" / "helm"
    script.parent.mkdir()
    script.write_text(_FAKE_HELM)
    script.chmod(0o755)
    monkeypatch.setattr("olf.common.tooling.retry.time.sleep", lambda _seconds: None)
    return Helm(ProcessRunner(), SimpleNamespace(resolve=lambda _name: script))  # type: ignore[arg-type]  # resolver stub


def test_pull_chart_retries_a_failed_pull_and_names_the_tarball_like_helm(tmp_path: Path, fake_helm: Helm) -> None:
    cache = tmp_path / "charts"
    cache.mkdir()

    tarball = _infra._pull_chart(
        SimpleNamespace(helm=fake_helm),  # type: ignore[arg-type]  # only .helm is used
        cache,
        "jetstack/cert-manager",
        "v1.21.2",
        env={},
    )

    assert tarball == cache / "cert-manager-v1.21.2.tgz"
    assert tarball.exists()
    assert len((tmp_path / "bin" / "calls").read_text().splitlines()) == 2


def test_pull_chart_skips_a_cached_tarball(tmp_path: Path, fake_helm: Helm) -> None:
    cache = tmp_path / "charts"
    cache.mkdir()
    (cache / "cert-manager-v1.21.2.tgz").touch()

    _infra._pull_chart(SimpleNamespace(helm=fake_helm), cache, "jetstack/cert-manager", "v1.21.2", env={})  # type: ignore[arg-type]

    assert not (tmp_path / "bin" / "calls").exists()


def test_infra_templates_the_cached_tarball_not_the_repo_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / ".tmp/check-helm/charts"
    cache.mkdir(parents=True)
    for name in (
        "seaweedfs-4.48.0",
        "polaris-1.8.0",
        "trino-1.42.2",
        "dagster-1.13.7",
        "superset-0.15.5",
        "traefik-41.6.1",
        "cert-manager-v1.21.2",
    ):
        (cache / f"{name}.tgz").touch()
    commands: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> CommandResult:
        commands.append(argv)
        return CommandResult(tuple(argv), 0, "polaris.persistence.type=relational-jdbc", "", 0.0)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a fully cached run must not touch the chart repositories")

    tools = SimpleNamespace(
        runner=SimpleNamespace(run=run),
        resolver=SimpleNamespace(resolve=lambda name: Path(name)),
        terraform=SimpleNamespace(init=lambda *_args, **_kwargs: None),
        helm=SimpleNamespace(repo_add=forbidden, repo_update=forbidden, pull=forbidden),
    )
    monkeypatch.setattr(_infra.Toolkit, "default", classmethod(lambda _cls: tools))

    _infra.infra(repo_root=str(tmp_path))

    templates = [argv for argv in commands if argv[:2] == ["helm", "template"]]
    assert len(templates) == 7
    assert all(Path(argv[3]).parent == cache and argv[3].endswith(".tgz") for argv in templates)
    assert all("--version" not in argv for argv in templates)
