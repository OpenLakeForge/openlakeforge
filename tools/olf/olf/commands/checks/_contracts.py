"""`olf check contracts`: parsed provider-contract validation."""

from __future__ import annotations

import typer

from olf.commands.checks._shared import _distribution_root_for, _root


def contracts(repo_root: str = typer.Option("", "--repo-root", help="Checkout or project root to validate.")) -> None:
    """Run the existing parsed provider-contract validation."""
    from olf import contracts_check
    from olf.project import ProjectSpec

    root = _root(repo_root)
    project = ProjectSpec(root=root, distribution_root=_distribution_root_for(root))
    report = contracts_check.run_contracts_check(project.root, distribution_root=project.distribution_root)
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)
