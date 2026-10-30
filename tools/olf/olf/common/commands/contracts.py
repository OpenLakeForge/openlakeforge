"""Offline provider-contract validation."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="Offline provider-contract validation.")


@app.command("check")
def contracts_check(
    repo_root: str = typer.Option(".", "--repo-root", help="Checkout or project root to validate."),
) -> None:
    """Behavioral contract validation: HCL structure, descriptors, rendered outputs."""
    from olf.common import contracts_check as contracts_check_module
    from olf.common.distribution import runtime_layout

    root = Path(repo_root).resolve()
    # An explicit --repo-root selecting a complete, separate checkout (its
    # own infra/terraform) must validate that checkout's own payload, not
    # the executing olf's — mirrors `olf check contracts`'s identical fix.
    distribution_root = root if (root / "infra" / "terraform").is_dir() else runtime_layout().distribution_root
    report = contracts_check_module.run_contracts_check(root, distribution_root=distribution_root)
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)
