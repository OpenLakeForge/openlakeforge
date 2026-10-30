"""Repository validation commands replacing ``scripts/test/*.sh``.

One module per `olf check` target; this package only registers them.
"""

from __future__ import annotations

import typer

from olf.common.commands.checks._components import components, lockfiles
from olf.common.commands.checks._contracts import contracts
from olf.common.commands.checks._dbt import dbt
from olf.common.commands.checks._docs import docs
from olf.common.commands.checks._flows import flows
from olf.common.commands.checks._infra import infra
from olf.common.commands.checks._project_code import project_code
from olf.common.commands.checks._security import security
from olf.common.commands.checks._skills import skills
from olf.common.commands.checks._structure import structure

app = typer.Typer(help="Repository validation and contributor checks.")

app.command("structure")(structure)
app.command("docs")(docs)
app.command("contracts")(contracts)
app.command("components")(components)
app.command("flows")(flows)
app.command("infra")(infra)
app.command("lockfiles")(lockfiles)
app.command("project-code")(project_code)
app.command("dbt")(dbt)
app.command("security")(security)
app.command("skills")(skills)


@app.command("all")
def all_checks(
    ctx: typer.Context,
    repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate."),
) -> None:
    """Run the complete contributor and release-readiness gate."""
    structure(repo_root)
    docs(repo_root)
    skills(ctx, repo_root)
    components(repo_root)
    contracts(repo_root)
    flows(repo_root)
    infra(repo_root)
    project_code(repo_root)
    dbt(repo_root)
    lockfiles(repo_root)
