"""The CLI tree keeps one visible home per command and every pre-2b path alive (#536)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import typer
from typer.core import TyperGroup

from olf.cli import app

# Every leaf path of the tree before `olf infra` existed (80 commands).
_PATHS_FILE = Path(__file__).parents[1] / "fixtures/cli_paths_pre_2b.txt"
PRE_2B_PATHS = [tuple(line.split()) for line in _PATHS_FILE.read_text().splitlines()]


def _leaves(
    group: TyperGroup, path: tuple[str, ...] = (), hidden: bool = False
) -> Iterator[tuple[tuple[str, ...], Callable[..., object], bool]]:
    for name, command in group.commands.items():
        if isinstance(command, TyperGroup):
            yield from _leaves(command, (*path, name), hidden or command.hidden)
        else:
            assert command.callback is not None
            # Typer wraps each registered function afresh per registration;
            # `__wrapped__` is the function itself.
            yield (*path, name), command.callback.__wrapped__, hidden or command.hidden


LEAVES = list(_leaves(typer.main.get_group(app)))
BY_PATH = {path: callback for path, callback, _ in LEAVES}


def test_every_leaf_has_exactly_one_visible_path() -> None:
    visible = defaultdict(list)
    for path, callback, hidden in LEAVES:
        if not hidden:
            visible[callback].append(" ".join(path))
    assert {c.__qualname__: p for c, p in visible.items() if len(p) > 1} == {}
    assert {callback for _, callback, _ in LEAVES} == set(visible)


def test_visible_infra_commands_live_under_olf_infra() -> None:
    misplaced = [
        " ".join(path)
        for path, callback, hidden in LEAVES
        if not hidden and callback.__module__.startswith("olf.infra.") != (path[0] == "infra")
    ]
    assert misplaced == []


@pytest.mark.parametrize("path", PRE_2B_PATHS, ids=" ".join)
def test_pre_2b_path_reaches_its_new_home(path: tuple[str, ...]) -> None:
    assert path in BY_PATH
    home = ("infra", *path) if ("infra", *path) in BY_PATH else path
    assert BY_PATH[path] is BY_PATH[home]
