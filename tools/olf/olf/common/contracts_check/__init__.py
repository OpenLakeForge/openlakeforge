"""Behavioral provider-contract validation for `olf contracts check`.

Replaces `scripts/test/check-contracts.sh`'s source-text grepping with
structured checks: parsed Terraform HCL, the canonical domain model plus
JSON Schema, and the Floe project layout, and parsed Helm values. There is no shell left to
cover — `olf check structure` rejects tracked `.sh` files (ADR 0008). Deploy
phase ordering is covered separately by
`tools/olf/tests/infra/test_deployment_engine.py`, not by this module.
"""

from __future__ import annotations

from pathlib import Path

from olf.common.contracts_check._descriptors import (
    _check_deployment_profile_schema_conformance,
    _check_descriptor_schema_conformance,
    descriptor_schema_errors,
    profile_schema_errors,
)
from olf.common.contracts_check._governance import _check_governance_conformance
from olf.common.contracts_check._hcl import _check_hcl_phase_two_invariants, _check_hcl_structured_contracts
from olf.common.contracts_check._rendered import (
    _check_floe_contract_structure,
    _check_helm_values_as_data,
    _check_silver_load_modes,
)
from olf.common.contracts_check._report import CheckResult, ContractsCheckReport

__all__ = [
    "CheckResult",
    "ContractsCheckReport",
    "descriptor_schema_errors",
    "profile_schema_errors",
    "run_contracts_check",
]


def run_contracts_check(
    repo_root: str | Path = ".", *, distribution_root: str | Path | None = None
) -> ContractsCheckReport:
    """Run every behavioral contract check against `repo_root`.

    `distribution_root` is where the immutable platform payload lives —
    `infra/terraform`, `infra/helm`, `docs/schema` — and
    defaults to `repo_root` for a source checkout, where the two coincide. An
    installed project's `repo_root` has only `lakehouse_code/` (ADR 0009), so
    every payload-owned check below runs against `distribution_root` instead;
    the Floe project under `lakehouse_code/silver/` remains project-owned and
    always reads from `repo_root`.
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo root does not exist: {root}")
    dist_root = Path(distribution_root).resolve() if distribution_root is not None else root

    report = ContractsCheckReport()
    report.results.append(_check_descriptor_schema_conformance(root, schema_root=dist_root / "docs" / "schema"))
    report.results.append(_check_governance_conformance(root, schema_root=dist_root / "docs" / "schema"))
    report.results.append(
        _check_deployment_profile_schema_conformance(
            root, schema_root=dist_root / "docs" / "schema", distribution_root=dist_root
        )
    )
    report.results.append(_check_hcl_structured_contracts(dist_root))
    report.results.append(_check_hcl_phase_two_invariants(dist_root))
    report.results.append(_check_floe_contract_structure(root))
    report.results.append(_check_silver_load_modes(root))
    report.results.append(_check_helm_values_as_data(dist_root))
    return report
