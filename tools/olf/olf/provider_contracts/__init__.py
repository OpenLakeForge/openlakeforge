"""Stage-aware provider-contract parsing and compatibility adaptation.

Terraform remains the source of physical provider values. This module owns the
typed boundary that validates those values against a resolved deployment
topology before a runtime consumes them.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from olf.profile import DeploymentTopology
from olf.provider_contracts._model import CodeLocation, ProviderContracts, SharedPlatformContract, StageContract
from olf.provider_contracts._v2 import _adapt_v2
from olf.provider_contracts._v3 import _parse_v3
from olf.provider_contracts._validation import (
    SUPPORTED_SCHEMA_VERSIONS,
    V2_SCHEMA_VERSION,
    V3_SCHEMA_VERSION,
    ProviderContractError,
    aws_catalog_name,
)

__all__ = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "V2_SCHEMA_VERSION",
    "V3_SCHEMA_VERSION",
    "CodeLocation",
    "ProviderContractError",
    "ProviderContracts",
    "SharedPlatformContract",
    "StageContract",
    "aws_catalog_name",
    "parse_provider_contracts",
]


def parse_provider_contracts(
    payload: Mapping[str, Any], topology: DeploymentTopology | None = None, *, distribution_root: Path | None = None
) -> ProviderContracts:
    """Parse v2/v3 provider output, rejecting unknown versions and shapes.

    `distribution_root` locates `release/identity-roles.yaml` for a v3 payload;
    it defaults to the environment's (`olf.config.distribution_root`).
    """
    schema_version = payload.get("schema_version")
    if schema_version == V2_SCHEMA_VERSION:
        return _adapt_v2(payload)
    if schema_version == V3_SCHEMA_VERSION:
        return _parse_v3(payload, topology, distribution_root)
    raise ProviderContractError(
        f"provider_contracts.schema_version {schema_version!r} is unsupported; "
        f"expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}"
    )
