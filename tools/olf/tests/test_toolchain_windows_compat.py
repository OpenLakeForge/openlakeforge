"""`olf.toolchain.manager`/`download` must import cleanly on a platform
without `fcntl` (POSIX-only, e.g. Windows), so `Platform.detect()` gets the
chance to raise its own actionable `UnsupportedPlatformError` instead of a
raw `ModuleNotFoundError` interrupting the import before that check ever
runs.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.mark.parametrize("module_name", ["olf.toolchain.manager", "olf.toolchain.download"])
def test_module_imports_without_fcntl_available(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    monkeypatch.setitem(sys.modules, "fcntl", None)
    for name in ("olf.toolchain.manager", "olf.toolchain.download"):
        sys.modules.pop(name, None)

    try:
        module = importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)

    assert module is not None
