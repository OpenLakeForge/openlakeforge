"""Repository validation commands replacing ``scripts/test/*.sh``.

One module per `olf check` target; this package only registers them.
"""

from __future__ import annotations

import typer

from olf.commands.checks._components import components, lockfiles
from olf.commands.checks._contracts import contracts
from olf.commands.checks._dbt import dbt
from olf.commands.checks._infra import infra
from olf.commands.checks._project_code import project_code
from olf.commands.checks._structure import structure

app = typer.Typer(help="Repository validation and contributor checks.")

app.command("structure")(structure)
app.command("contracts")(contracts)
app.command("components")(components)
app.command("infra")(infra)
app.command("lockfiles")(lockfiles)
app.command("project-code")(project_code)
app.command("dbt")(dbt)


@app.command("all")
def all_checks(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Run the complete contributor and release-readiness gate."""
    structure(repo_root)
    components(repo_root)
    contracts(repo_root)
    infra(repo_root)
    project_code(repo_root)
    dbt(repo_root)
    lockfiles(repo_root)
