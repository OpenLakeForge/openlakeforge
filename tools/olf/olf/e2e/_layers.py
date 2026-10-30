"""Optional governance/analytics platform layer gating."""

from __future__ import annotations

from olf.e2e._shell import E2EConfig, E2EError, Layer, load_provider_contracts_or_raise
from olf.e2e._trino import stage_catalog_name


def configured_layers(cfg: E2EConfig) -> dict[Layer, bool]:
    """Read enabled platform layers once from the environment contract.

    Every current root emits a v3 (`schema_version` "3.0.0") contract: the
    stage-aware family nests `reporting`/`governance` inside each stage's own
    block (aws-poc/local/azure contracts.tf), present only when that stage's
    analytics/governance capability is on - there is no top-level "enabled"
    field to read, key absence *is* the disabled signal. Falls back to the
    legacy v2 shape's top-level `reporting`/`governance` blocks (each with an
    explicit `enabled` bool) for any contract still on that schema.
    """
    provider_contracts = load_provider_contracts_or_raise(cfg)
    stages = provider_contracts.get("stages")
    if isinstance(stages, dict) and stages:
        # A multi-stage v3 contract's dict order is not "the stage under
        # test" - resolve the stage this run actually targets the same way
        # _trino.py does (every stage's Trino catalog is named
        # "lakehouse_<stage>"), or a --stage prod run against a dev+prod
        # deployment would silently gate on dev's capabilities instead.
        stage_name = stage_catalog_name(cfg).removeprefix("lakehouse_")
        stage = stages.get(stage_name)
        if stage is None:
            raise E2EError(f"provider_contracts.stages has no entry for stage {stage_name!r}.")
        return {
            "governance": "governance" in stage,
            "analytics": "reporting" in stage,
        }
    layers: dict[Layer, dict] = {
        "governance": provider_contracts.get("governance") or {},
        "analytics": provider_contracts.get("reporting") or {},
    }
    enabled: dict[Layer, bool] = {}
    for layer, contract in layers.items():
        value = contract.get("enabled", True)
        if not isinstance(value, bool):
            raise E2EError(f"provider_contracts.{layer}.enabled must be a boolean.")
        enabled[layer] = value
    return enabled
