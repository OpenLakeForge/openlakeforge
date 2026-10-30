"""Read the Terraform `provider_contracts` output, or the file that stands in for it."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olf.contract_validation import SUPPORTED_SCHEMA_VERSIONS, ProviderContractError
from olf.tooling.terraform import external_state_options

if TYPE_CHECKING:
    from olf.tooling.resolver import ExecutableResolver

PROVIDER_CONTRACTS_FILE_ENV = "OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE"


def load_provider_contracts(
    terraform_dir: str,
    *,
    environ: Mapping[str, str] | None = None,
    honor_contract_file: bool = True,
    strict_state: bool = False,
    resolver: ExecutableResolver | None = None,
) -> dict[str, Any] | None:
    """Read the Terraform provider_contracts output, or None before apply.

    Returns the raw v2 or v3 payload; version dispatch belongs to
    `parse_provider_contracts`, and a v3 payload only resolves once its
    caller supplies a `DeploymentTopology` and selects a stage
    (`build_contract_env`). The local platform root still exports v2 -- #114
    switches it to v3 once every stage owns its storage and catalog.

    Only a missing executable (`ExecutableNotFoundError`) is treated as
    "not applied yet" - matching this function's pre-#127 behaviour, where
    `terraform` simply not being on `PATH` fell into the same `OSError`
    branch below. A managed-toolchain provisioning failure
    (`ToolchainError`: a bad digest, a broken download, a malformed
    catalog) is a real operational problem, not an unapplied-state signal,
    and must propagate rather than be silently treated as "no contracts
    yet" - a caller that swallowed it would fall back to defaults (e.g.
    enabling governance/analytics for what should be a slim deployment)
    instead of failing closed.

    With OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE set, the contract is read from
    that file instead (`olf platform contract` writes it): a CI job deploying
    a project revision to an applied platform has no access to the
    platform's Terraform state (#119). The file was named deliberately, so a
    missing or malformed one fails closed rather than reading as "not
    applied yet".

    `strict_state` narrows "not applied" to Terraform's own "output not
    found" answer: a state Terraform cannot read (corrupt, backend not
    initialised) raises instead. Pre-apply callers keep the lenient default
    because an uninitialised remote backend is exactly what they see first.

    A caller that already holds a `Toolkit` passes its `resolver`, so the
    contract read finds `terraform` exactly as the caller's own commands do.
    Without one a resolver is built from `environ`, and a scoped `environ`
    that omits `OLF_TOOLCHAIN_MODE` selects managed provisioning (#233).
    """
    base_environ = environ if environ is not None else os.environ
    # Scoped provider envs drop ambient variables, so fall back to the process
    # env: this selector is set by the CI job, never by a provider.
    contract_file = honor_contract_file and (
        base_environ.get(PROVIDER_CONTRACTS_FILE_ENV) or os.environ.get(PROVIDER_CONTRACTS_FILE_ENV)
    )
    if contract_file:
        try:
            contracts = json.loads(Path(contract_file).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderContractError(f"{PROVIDER_CONTRACTS_FILE_ENV}={contract_file}: {exc}") from exc
        if not isinstance(contracts, dict):
            raise ProviderContractError(f"{PROVIDER_CONTRACTS_FILE_ENV}={contract_file}: not a JSON object")
    else:
        contracts = _terraform_provider_contracts(
            terraform_dir, environ=environ, strict_state=strict_state, resolver=resolver
        )
        if contracts is None:
            return None
    schema_version = contracts.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ProviderContractError(
            f"provider_contracts.schema_version {schema_version!r} is unsupported; "
            f"expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}"
        )
    return contracts


def _terraform_provider_contracts(
    terraform_dir: str,
    *,
    environ: Mapping[str, str] | None,
    strict_state: bool = False,
    resolver: ExecutableResolver | None = None,
) -> dict[str, Any] | None:
    from olf.deployment.errors import ExecutableNotFoundError
    from olf.tooling.resolver import build_resolver

    try:
        if resolver is None:
            resolver = build_resolver(environ=environ) if environ is not None else build_resolver()
        terraform = str(resolver.resolve("terraform"))
    except ExecutableNotFoundError:
        return None
    # An installed distribution's state lives under OLF_HOME, not next to
    # terraform_dir - without this, `terraform output` reads the read-only
    # payload's default (absent) state and this always returns None, even
    # after a successful `terraform apply` (see olf.tooling.terraform.
    # external_state_options, which Terraform._run also uses).
    base_environ = environ if environ is not None else os.environ
    overlay, state_path = external_state_options(Path(terraform_dir), base_environ, create=False)
    args = ["output", "-json", "provider_contracts"]
    if state_path is not None:
        args.insert(1, f"-state={state_path}")
    if state_path is not None:
        command_env: dict[str, str] | None = {**dict(base_environ), **overlay}
    elif environ is not None:
        command_env = dict(environ)
    else:
        command_env = None
    try:
        result = subprocess.run(
            [terraform, f"-chdir={terraform_dir}", *args],
            capture_output=True,
            text=True,
            check=True,
            env=command_env,
        )
    except OSError:
        return None
    except subprocess.CalledProcessError as exc:
        # Terraform answers "not found" both for a state without this output
        # and for no state at all; anything else means it could not read one.
        if not strict_state or 'Output "provider_contracts" not found' in (exc.stderr or ""):
            return None
        raise ProviderContractError(
            f"terraform output provider_contracts failed in {terraform_dir}: {(exc.stderr or '').strip()}"
        ) from exc
    try:
        contracts = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if strict_state:
            raise ProviderContractError(f"terraform output provider_contracts in {terraform_dir}: {exc}") from exc
        return None
    if not isinstance(contracts, dict):
        if strict_state:
            raise ProviderContractError(f"terraform output provider_contracts in {terraform_dir}: not a JSON object")
        return None
    return contracts
