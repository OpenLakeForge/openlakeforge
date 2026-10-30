"""Provider-contract runtime environment helpers."""

from __future__ import annotations

import os

import typer

from olf.common import config, contract_state
from olf.common.log import fail
from olf.infra import contracts as contracts_module

app = typer.Typer(help="Provider-contract runtime environment helpers.")


@app.command("env")
def contracts_env(
    terraform_dir: str = typer.Option(
        "infra/terraform/environments/local",
        "--terraform-dir",
        help="Terraform root exposing the provider_contracts output.",
    ),
) -> None:
    """Print `export`/`unset` lines for the runtime contract environment.

    Falls back to local defaults when the Terraform stack has not been applied
    yet. Intended for `eval` from scripts/contracts/load-runtime-env.sh.
    """
    from olf.common.deployment.errors import DeploymentError

    try:
        provider_contracts = contract_state.load_provider_contracts(terraform_dir)
    except DeploymentError as exc:
        # A managed-toolchain provisioning failure (#127) resolving
        # terraform is a real operational problem, not "not applied yet" -
        # load_provider_contracts() already distinguishes the two and lets
        # this propagate; normalize it into the same clean CLI failure
        # every other olf command produces instead of a raw traceback.
        raise typer.Exit(code=fail(str(exc))) from exc
    exports, unsets = contracts_module.build_contract_env(os.environ, provider_contracts, repo_root=config.repo_root())
    output = contracts_module.render_shell_exports(exports, unsets)
    if output:
        typer.echo(output)


