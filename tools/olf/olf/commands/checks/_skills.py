"""`olf check skills`: the agent skills under `skills/` stay true to `olf`.

A skill is read by an agent that runs what it says without checking `--help`
first, so a renamed command or flag turns into a confident wrong action. Every
`olf` invocation a skill shows is resolved against the real command tree here.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import typer
import typer.core
import yaml

from olf.commands._shared import fail
from olf.commands.checks._docs import _links
from olf.commands.checks._shared import _root

SKILLS_DIR = "skills"

# The Agent Skills specification (agentskills.io/specification): any other
# top-level field is ignored by some agents and rejected by others.
_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NAME_MAX = 64
_DESCRIPTION_MAX = 1024

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_OLF = re.compile(r"(?<!`)`(olf [^`]+)`(?!`)")
_SUBSHELL_OLF = re.compile(r"\$\((olf [^)]*)\)")
_Command = typer.core.TyperGroup | typer.core.TyperCommand
_SHELL_OPERATORS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "(", ")"}


def skills(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Validate agent skill metadata, the `olf` commands they show, and their links."""
    errors = skill_errors(_root(repo_root))
    if errors:
        raise typer.Exit(code=fail("\n".join(errors)))
    typer.echo("Agent skills are valid.")


def skill_errors(root: Path) -> list[str]:
    # olf.cli imports this package to register the check.
    from olf.cli import app

    base = root / SKILLS_DIR
    if not base.is_dir():
        return [f"{SKILLS_DIR}/: directory is missing"]
    cli = typer.main.get_group(app)
    errors: list[str] = []
    for directory in sorted(path for path in base.iterdir() if path.is_dir()):
        skill = directory / "SKILL.md"
        if not skill.is_file():
            errors.append(f"{_relative(root, directory)}: has no SKILL.md")
            continue
        errors.extend(_frontmatter_errors(root, skill))
        for page in sorted(directory.rglob("*.md")):
            errors.extend(_command_errors(root, page, cli))
            errors.extend(_link_errors(root, directory, page))
    return errors


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _frontmatter_errors(root: Path, skill: Path) -> list[str]:
    where = _relative(root, skill)
    text = skill.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return [f"{where}: must open with YAML frontmatter between `---` lines"]
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [f"{where}: frontmatter is not YAML: {exc}"]
    if not isinstance(meta, dict):
        return [f"{where}: frontmatter must be a mapping"]
    errors = [f"{where}: unknown frontmatter field `{key}`" for key in sorted(set(meta) - _FIELDS)]
    name = meta.get("name")
    if not isinstance(name, str) or not _NAME.match(name) or len(name) > _NAME_MAX:
        errors.append(f"{where}: `name` must be lowercase words joined by single hyphens, at most {_NAME_MAX} chars")
    elif name != skill.parent.name:
        errors.append(f"{where}: `name` {name!r} must match its directory {skill.parent.name!r}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{where}: `description` is required")
    elif len(description) > _DESCRIPTION_MAX:
        errors.append(f"{where}: `description` is longer than {_DESCRIPTION_MAX} chars")
    return errors


def _invocations(page: Path) -> list[tuple[int, str]]:
    """Every `olf` command line a page shows: fenced lines and inline code spans."""
    found: list[tuple[int, str]] = []
    fence: str | None = None
    continued: tuple[int, str] | None = None
    for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
        opener = _FENCE.match(line)
        if opener and fence is None:
            fence = opener.group(1)
            continue
        if opener and fence and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
            fence = None
            continue
        if fence is None:
            found.extend((number, command) for command in _INLINE_OLF.findall(line))
            continue
        if continued is not None:
            line = continued[1] + " " + line.strip()
            number = continued[0]
            continued = None
        if line.rstrip().endswith("\\"):
            continued = (number, line.rstrip()[:-1])
            continue
        stripped = line.strip()
        if stripped.startswith("olf "):
            found.append((number, stripped))
        found.extend((number, command) for command in _SUBSHELL_OLF.findall(line))
    return found


def _command_errors(root: Path, page: Path, cli: _Command) -> list[str]:
    where = _relative(root, page)
    errors: list[str] = []
    for number, command in _invocations(page):
        problem = _resolve(cli, command)
        if problem:
            errors.append(f"{where}:{number}: {problem}: `{command}`")
    return errors


def _resolve(cli: _Command, command: str) -> str | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        return f"cannot parse ({exc})"
    for index, token in enumerate(tokens):
        if token in _SHELL_OPERATORS:
            tokens = tokens[:index]
            break
    current: _Command = cli
    path = ["olf"]
    for token in tokens[1:]:
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if name != "--help" and name not in _option_names(current):
                return f"`{' '.join(path)}` has no option {name}"
        elif isinstance(current, typer.core.TyperGroup):
            sub = current.commands.get(token)
            if not isinstance(sub, _Command):
                return f"`{' '.join(path)}` has no command {token!r}"
            current = sub
            path.append(token)
    if isinstance(current, typer.core.TyperGroup):
        return f"`{' '.join(path)}` needs a subcommand"
    return None


def _option_names(command: _Command) -> set[str]:
    return {
        name
        for param in command.params
        if isinstance(param, typer.core.TyperOption)
        for name in (*param.opts, *param.secondary_opts)
    }


def _link_errors(root: Path, skill_dir: Path, page: Path) -> list[str]:
    """Links resolve inside the skill: installers copy the skill directory and nothing else."""
    where = _relative(root, page)
    errors: list[str] = []
    for number, target in _links(page):
        path_part = target.partition("#")[0]
        if not path_part:
            continue
        resolved = (page.parent / path_part).resolve()
        if not resolved.is_relative_to(skill_dir.resolve()):
            errors.append(f"{where}:{number}: link leaves the skill directory: {target}")
        elif not resolved.exists():
            errors.append(f"{where}:{number}: link target does not exist: {target}")
    return errors
