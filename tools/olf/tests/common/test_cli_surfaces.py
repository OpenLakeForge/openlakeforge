"""The CLI tree keeps one visible home per command and every pre-2b path alive (#536)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import typer
from typer.core import TyperGroup

from olf.cli import app
from olf.common import moved

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
            yield (*path, name), command.callback, hidden or command.hidden


LEAVES = list(_leaves(typer.main.get_group(app)))
BY_PATH = {path: callback for path, callback, _ in LEAVES}


def _command(callback: Callable[..., object]) -> Callable[..., object]:
    # Typer wraps each registered function afresh per registration, and an old
    # path adds its warning shim; peel both to reach the function itself.
    function = callback.__wrapped__  # type: ignore[attr-defined]
    return function.__wrapped__ if function.__code__.co_filename == moved.__file__ else function  # type: ignore[attr-defined,no-any-return]


def test_every_leaf_has_exactly_one_visible_path() -> None:
    visible = defaultdict(list)
    for path, callback, hidden in LEAVES:
        if not hidden:
            visible[_command(callback)].append(" ".join(path))
    assert {c.__qualname__: p for c, p in visible.items() if len(p) > 1} == {}
    assert {_command(callback) for _, callback, _ in LEAVES} == set(visible)


def test_visible_infra_commands_live_under_olf_infra() -> None:
    misplaced = [
        " ".join(path)
        for path, callback, hidden in LEAVES
        if not hidden and _command(callback).__module__.startswith("olf.infra.") != (path[0] == "infra")
    ]
    assert misplaced == []


@pytest.mark.parametrize("path", PRE_2B_PATHS, ids=" ".join)
def test_pre_2b_path_reaches_its_new_home(path: tuple[str, ...]) -> None:
    assert path in BY_PATH
    home = ("infra", *path) if ("infra", *path) in BY_PATH else path
    assert _command(BY_PATH[path]) is _command(BY_PATH[home])


MOVED_PATHS = [path for path in PRE_2B_PATHS if ("infra", *path) in BY_PATH]


def _warning(path: tuple[str, ...], monkeypatch: pytest.MonkeyPatch) -> str | None:
    """The warning the command at `path` prints before running, without running it."""

    class Stop(Exception):
        pass

    printed: list[str] = []

    def warn(message: str) -> None:
        printed.append(message)
        raise Stop

    monkeypatch.setattr("olf.common.moved.log.warn", warn)
    try:
        BY_PATH[path]()
    except Stop:
        return printed[0]
    return None


@pytest.mark.parametrize("path", [p for p in MOVED_PATHS if p != ("query",)], ids=" ".join)
def test_old_path_warns_and_names_its_new_path(path: tuple[str, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    assert _warning(path, monkeypatch) == (
        f"this command moved to `olf infra {' '.join(path)}`; the old path will be removed in a later release"
    )


def _has_warning_shim(callback: Callable[..., object] | None) -> bool:
    while callback is not None:
        if callback.__code__.co_filename == moved.__file__:
            return True
        callback = getattr(callback, "__wrapped__", None)
    return False


def test_new_paths_and_the_query_alias_do_not_warn() -> None:
    # `olf query` stays a top-level command once per-person access lands (ADR 0020).
    silent = [
        " ".join(path)
        for path, callback, _ in LEAVES
        if path[0] == "infra" or path == ("query",)
        if _has_warning_shim(callback)
    ]
    assert silent == []
