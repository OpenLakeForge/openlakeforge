"""`olf check contracts`: parsed provider-contract validation."""

from __future__ import annotations

import contextlib
import json
import sys

import typer

from olf.common.commands.checks._shared import _distribution_root_for, _root


def contracts(
    repo_root: str = typer.Option("", "--repo-root", help="Checkout or project root to validate."),
    json_output: bool = typer.Option(
        False, "--json", help="Print the report as JSON on stdout; progress goes to stderr."
    ),
) -> None:
    """Run the existing parsed provider-contract validation."""
    from olf.common import contracts_check
    from olf.common.project import ProjectSpec

    root = _root(repo_root)
    project = ProjectSpec(root=root, distribution_root=_distribution_root_for(root))

    progress = contextlib.redirect_stdout(sys.stderr) if json_output else contextlib.nullcontext()
    try:
        with progress:
            report = contracts_check.run_contracts_check(project.root, distribution_root=project.distribution_root)
    except Exception as exc:
        if json_output:
            error_report = {
                "command": "check contracts",
                "ok": False,
                "items": [{
                    "id": "execution_error",
                    "status": "fail",
                    "message": str(exc),
                    "remedy": None,
                }]
            }
            typer.echo(json.dumps(error_report, indent=2))
            raise typer.Exit(code=1) from exc
        else:
            raise

    typer.echo(json.dumps(report.as_dict(), indent=2) if json_output else report.render())
    if not report.ok:
        raise typer.Exit(code=1)
