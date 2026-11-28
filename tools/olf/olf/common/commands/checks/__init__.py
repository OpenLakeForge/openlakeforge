"""Repository validation commands replacing ``scripts/test/*.sh``.

One module per `olf check` target; this package only registers them.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from olf.common.commands.checks._components import components, lockfiles
from olf.common.commands.checks._contracts import contracts
from olf.common.commands.checks._dbt import dbt
from olf.common.commands.checks._disruption import disruption
from olf.common.commands.checks._docs import docs
from olf.common.commands.checks._flows import flows
from olf.common.commands.checks._infra import infra
from olf.common.commands.checks._json import JSON_OPTION, run_targets, with_json
from olf.common.commands.checks._project_code import project_code
from olf.common.commands.checks._resources import resources
from olf.common.commands.checks._security import security
from olf.common.commands.checks._skills import skills
from olf.common.commands.checks._structure import structure

app = typer.Typer(help="Repository validation and contributor checks.")

_TARGETS: dict[str, Callable[..., None]] = {
    "structure": structure,
    "docs": docs,
    "contracts": contracts,
    "components": components,
    "flows": flows,
    "infra": infra,
    "lockfiles": lockfiles,
    "resources": resources,
    "disruption": disruption,
    "project-code": project_code,
    "dbt": dbt,
    "security": security,
    "skills": skills,
}
for _name, _target in _TARGETS.items():
    app.command(_name)(with_json(_name, _target))


@app.command("all")
def all_checks(
    ctx: typer.Context,
    repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate."),
    json_output: bool = JSON_OPTION,
) -> None:
    """Run the complete contributor and release-readiness gate."""
    targets: list[tuple[str, Callable[[], None]]] = [
        ("structure", lambda: structure(repo_root)),
        ("docs", lambda: docs(repo_root)),
        ("skills", lambda: skills(ctx, repo_root)),
        ("components", lambda: components(repo_root)),
        ("contracts", lambda: contracts(repo_root, strict=False)),
        ("flows", lambda: flows(repo_root)),
        ("infra", lambda: infra(repo_root)),
        ("resources", lambda: resources(repo_root)),
        ("disruption", lambda: disruption(repo_root)),
        ("project-code", lambda: project_code(repo_root)),
        ("dbt", lambda: dbt(repo_root)),
        ("lockfiles", lambda: lockfiles(repo_root)),
    ]
    if json_output:
        run_targets("check all", targets)
        return
    for _, target in targets:
        target()
