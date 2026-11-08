"""The Polaris and OpenMetadata bootstraps, run by the platform phase once `terraform apply` has finished (#562).

Operator-side, as `olf infra catalog sync-namespaces` is: no olf image runs in the cluster. Their
inputs come from the applied provider contract alone, never from `lakehouse_code/`, so the platform
phase stays descriptor-free (ADR 0002).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from olf.common.deployment.errors import DeploymentPreconditionError

if TYPE_CHECKING:
    from olf.infra.deployment.engine import DeploymentProvider


def _applied_contract(provider: DeploymentProvider) -> tuple[Mapping[str, Any], dict[str, str]]:
    from olf.common import contract_state

    context = provider.context
    # The ambient environment under the provider's own, as the contract environment of the other phases is.
    environ = {**os.environ, **provider.env}
    contract_dir = Path(environ.get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", context.paths.platform_terraform_dir))
    raw = contract_state.load_provider_contracts(str(contract_dir.resolve()), environ=environ)
    if raw is None:
        raise DeploymentPreconditionError(f"provider contracts are unavailable from {contract_dir} after the apply.")
    return raw, environ


def bootstrap_polaris(provider: DeploymentProvider) -> None:
    from olf.common.provider_contracts import ProviderContractError, parse_provider_contracts
    from olf.infra import k8s, polaris_bootstrap
    from olf.infra.polaris import PolarisError

    raw, environ = _applied_contract(provider)
    context = provider.context
    try:
        contracts = parse_provider_contracts(raw, context.topology, distribution_root=context.paths.distribution_root)
        polaris_bootstrap.bootstrap_from_contract(contracts, environ=environ, remove_stages=context.allow_stage_removal)
    except (ProviderContractError, PolarisError, k8s.KubectlError) as exc:
        raise DeploymentPreconditionError(f"Polaris bootstrap: {exc}") from exc


def bootstrap_openmetadata(provider: DeploymentProvider) -> None:
    from olf.common.provider_contracts import ProviderContractError, parse_provider_contracts
    from olf.infra.commands import openmetadata

    raw, environ = _applied_contract(provider)
    context = provider.context
    try:
        contracts = parse_provider_contracts(raw, context.topology, distribution_root=context.paths.distribution_root)
        openmetadata.bootstrap_from_contract(contracts, environ=environ)
    except ProviderContractError as exc:
        raise DeploymentPreconditionError(f"OpenMetadata bootstrap: {exc}") from exc
    except typer.Exit as exc:
        raise DeploymentPreconditionError("OpenMetadata bootstrap failed (see the ERROR above).") from exc
