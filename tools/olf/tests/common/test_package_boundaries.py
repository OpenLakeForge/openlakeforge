"""`olf.common` needs no permissions; `olf.platform` builds only on it (ADR 0020).

Also: `subprocess` is imported only by `olf.common.tooling` (#560).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import olf

COMMON = Path(olf.__file__).parent / "common"
PLATFORM = Path(olf.__file__).parent / "platform"
OLF = Path(olf.__file__).parent
TOOLING = OLF / "common" / "tooling"
# Runs stdlib-only inside the ingestion-base image (ConfigMap mount, no olf) and pipes raw bytes.
SUBPROCESS_ALLOWED = {OLF / "infra" / "openmetadata" / "role_sync_script.py"}
FORBIDDEN = ("olf.infra", "olf.platform", "olf.cli")


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
            found += [(node.lineno, f"{node.module}.{alias.name}") for alias in node.names]
    return found


def _violations(package: Path, forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(package.parent)}:{line} imports {module}"
        for path in sorted(package.rglob("*.py"))
        for line, module in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        if any(module == name or module.startswith(f"{name}.") for name in forbidden)
    ]


def test_common_imports_neither_infra_nor_platform_nor_cli() -> None:
    assert not _violations(COMMON, FORBIDDEN)


def test_platform_imports_neither_infra_nor_cli() -> None:
    assert PLATFORM.is_dir()
    assert not _violations(PLATFORM, ("olf.infra", "olf.cli"))


def _subprocess_uses(source: str) -> list[int]:
    """Line numbers of `import subprocess`, `from subprocess import ...` and `import_module("subprocess")`."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "subprocess" for a in node.names):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "subprocess":
            lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "subprocess"
        ):
            lines.append(node.lineno)
    return lines


def test_subprocess_is_imported_only_by_tooling() -> None:
    assert SUBPROCESS_ALLOWED <= set(OLF.rglob("*.py"))
    found = [
        f"{path.relative_to(OLF.parent)}:{line} uses subprocess; run commands through "
        "olf.common.tooling.process.ProcessRunner"
        for path in sorted(OLF.rglob("*.py"))
        if TOOLING not in path.parents and path not in SUBPROCESS_ALLOWED
        for line in _subprocess_uses(path.read_text(encoding="utf-8"))
    ]
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import os, subprocess as sp",
        "from subprocess import run",
        "def f():\n    import subprocess",
        'import importlib\nimportlib.import_module("subprocess")',
        'from importlib import import_module\nimport_module("subprocess")',
    ],
)
def test_subprocess_use_is_detected(source: str) -> None:
    assert _subprocess_uses(source)


def test_subprocess_lookalikes_are_not_flagged() -> None:
    assert not _subprocess_uses("import os\nfrom olf.common.tooling.process import ProcessRunner")
