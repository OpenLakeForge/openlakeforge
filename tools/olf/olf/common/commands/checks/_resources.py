"""`olf check resources`: every Terraform-defined container sets requests and a memory limit."""

from __future__ import annotations

import re
from pathlib import Path

import typer

from olf.common.commands.checks._shared import _root
from olf.common.log import fail

# Containers the rule cannot or should not hold. Key: "<path>:<container name>", value: the reason.
# Empty today; add an entry only with a reason a reviewer can accept.
EXEMPT: dict[str, str] = {}

# ponytail: a line scan, not a parser. python-hcl2 rejects several modules' files (a comment after a
# ternary, `:` in a string key), and `terraform fmt -check` (olf check infra) guarantees the
# indentation this relies on. A `dynamic "container"` block is not seen; none exist today.
_OPEN = re.compile(r"^(\s*)(container|init_container) \{$")
_NAME = re.compile(r'^\s*name\s*=\s*"([^"]*)"')


def _body(lines: list[str], start: int) -> list[str]:
    """Lines inside the block opened at `start`, up to the closing brace at the same indent."""
    indent = len(lines[start]) - len(lines[start].lstrip())
    closing = " " * indent + "}"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].rstrip() == closing), len(lines))
    return lines[start + 1 : end]


def _field(body: list[str], key: str) -> list[str] | str | None:
    """Lines of `key = { ... }` or `key { ... }` among the body; "expression" for `key = <expression>`
    (only literals are verified); None when absent."""
    for i, line in enumerate(body):
        if re.match(rf"^\s*{key}( = \{{| \{{)$", line):
            return _body(body, i)
        if re.match(rf"^\s*{key} = ", line):
            return "expression"
    return None


def _keys(lines: list[str]) -> set[str]:
    return {m.group(1) for line in lines if (m := re.match(r"^\s*(\w+)\s*=", line))}


def _missing(container: list[str]) -> list[str]:
    resources = _field(container, "resources")
    if resources is None:
        return ["resources"]
    if isinstance(resources, str):
        return []
    requests, limits = _field(resources, "requests"), _field(resources, "limits")
    missing: list[str] = []
    if requests is None:
        missing += ["requests.cpu", "requests.memory"]
    elif not isinstance(requests, str):
        missing += [f"requests.{key}" for key in ("cpu", "memory") if key not in _keys(requests)]
    if limits is None or (not isinstance(limits, str) and "memory" not in _keys(limits)):
        missing.append("limits.memory")
    return missing


def resource_errors(root: Path) -> list[str]:
    """One message per container under `infra/terraform/modules` lacking requests or a memory limit."""
    errors: list[str] = []
    for path in sorted((root / "infra/terraform/modules").rglob("*.tf")):
        where = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not _OPEN.match(line):
                continue
            body = _body(lines, i)
            name = next((m.group(1) for x in body if (m := _NAME.match(x))), "<unnamed>")
            missing = _missing(body)
            if missing and f"{where}:{name}" not in EXEMPT:
                errors.append(f"{where}:{i + 1}: container {name!r} lacks {', '.join(missing)}")
    return errors


def resources(
    repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate."),
    json_output: bool = typer.Option(False, "--json", help="Output JSON instead of human-readable text."),
) -> None:
    """Require requests (cpu, memory) and a memory limit on every Terraform container."""
    import contextlib
    import json
    import sys

    progress = contextlib.redirect_stdout(sys.stderr) if json_output else contextlib.nullcontext()
    try:
        with progress:
            errors = resource_errors(_root(repo_root))
    except Exception as exc:
        if json_output:
            error_report = {
                "command": "check resources",
                "ok": False,
                "items": [{
                    "id": "execution_error",
                    "status": "fail",
                    "message": str(exc),
                    "remedy": None,
                }]
            }
            typer.echo(json.dumps(error_report, indent=2))
            raise typer.Exit(1) from exc
        else:
            raise

    if json_output:
        result = {
            "command": "check resources",
            "ok": len(errors) == 0,
            "items": [
                {"id": f"resources_error_{i}", "status": "fail", "message": err, "remedy": None}
                for i, err in enumerate(errors, 1)
            ],
        }
        typer.echo(json.dumps(result, indent=2))
        if errors:
            raise typer.Exit(1)
    else:
        if errors:
            raise typer.Exit(code=fail("\n".join(errors)))
        typer.echo("Container resource checks passed.")
