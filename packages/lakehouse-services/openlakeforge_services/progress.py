"""How a library operation reports what it is doing, without printing itself."""

from __future__ import annotations

from typing import Protocol


class Progress(Protocol):
    """Where an operation reports progress; `olf`'s `log` module is one."""

    def step(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...


class _Silent:
    def step(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        pass


SILENT: Progress = _Silent()
