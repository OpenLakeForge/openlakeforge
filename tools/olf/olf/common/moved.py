"""Warning wrapper for the hidden aliases left behind by the `olf infra` move (#536)."""

from __future__ import annotations

import functools
from collections.abc import Callable

from olf.common import log


def moved_alias(command: Callable[..., None], new_path: str) -> Callable[..., None]:
    # functools.wraps exposes the command's signature, so Typer builds the
    # alias with the same options. stderr only: scripts parse stdout.
    @functools.wraps(command)
    def alias(**kwargs: object) -> None:
        log.warn(f"this command moved to `{new_path}`; the old path will be removed in a later release")
        command(**kwargs)

    return alias
