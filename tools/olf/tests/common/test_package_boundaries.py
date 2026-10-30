"""`olf.common` needs no permissions; `olf.platform` builds only on it (ADR 0020)."""

from __future__ import annotations

import ast
from pathlib import Path

import olf

COMMON = Path(olf.__file__).parent / "common"
PLATFORM = Path(olf.__file__).parent / "platform"
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
