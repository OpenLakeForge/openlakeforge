"""`olf.common` is the surface that needs no cluster or cloud permissions."""

from __future__ import annotations

import ast
from pathlib import Path

import olf

COMMON = Path(olf.__file__).parent / "common"
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


def test_common_imports_neither_infra_nor_platform_nor_cli() -> None:
    violations = [
        f"{path.relative_to(COMMON.parent)}:{line} imports {module}"
        for path in sorted(COMMON.rglob("*.py"))
        for line, module in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        if any(module == name or module.startswith(f"{name}.") for name in FORBIDDEN)
    ]
    assert not violations, "\n".join(violations)
