"""A `Route` whose session answers from the test, for e2e helpers that take the route they reach a service through."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from olf.infra import access


class FakeRoute(access.Route):
    """`session()` returns an object with the given `get` / `post` / `mount` callables."""

    def __init__(self, **session: Any) -> None:
        super().__init__("")
        object.__setattr__(self, "_session", session)

    def session(self) -> Any:  # type: ignore[override]  # a stand-in that answers only the verbs a test names
        return SimpleNamespace(**self._session)  # type: ignore[attr-defined]  # set in __init__
