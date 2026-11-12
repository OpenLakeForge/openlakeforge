from __future__ import annotations

from pathlib import Path

import pytest

from olf.common import config
from olf.common.commands import diagnostics


def test_tree_excludes_generated_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("hello\n")

    cached = tmp_path / ".venv" / "lib" / "package.py"
    cached.parent.mkdir(parents=True)
    cached.write_text("generated\n")

    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)

    terraform_file = tmp_path / ".terraform" / "cache.bin"
    terraform_file.parent.mkdir()
    terraform_file.write_text("cached\n")

    for name in (".git", "__pycache__", ".pytest_cache"):
        generated = tmp_path / name / "cache.txt"
        generated.parent.mkdir()
        generated.write_text("cached\n")

    nested_cache = tmp_path / "src" / "__pycache__" / "app.pyc"
    nested_cache.parent.mkdir()
    nested_cache.write_text("cached\n")

    nested_source = tmp_path / "src" / "pkg" / "module.py"
    nested_source.parent.mkdir()
    nested_source.write_text("hello\n")

    sibling_source = tmp_path / "tools" / "cli.py"
    sibling_source.parent.mkdir()
    sibling_source.write_text("hello\n")
    (tmp_path / "src.txt").write_text("hello\n")
    (tmp_path / "z.py").write_text("hello\n")
    (tmp_path / ".github").mkdir()

    diagnostics.tree()
    assert capsys.readouterr().out.splitlines() == [
        ".github",
        "src",
        "src/app.py",
        "src/pkg",
        "src/pkg/module.py",
        "src.txt",
        "tools",
        "tools/cli.py",
        "z.py",
    ]


def test_tree_excludes_git_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".git").write_text("gitdir: /some/worktree\n")
    (tmp_path / "app.py").write_text("hello\n")
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)

    diagnostics.tree()

    assert capsys.readouterr().out.splitlines() == ["app.py"]
