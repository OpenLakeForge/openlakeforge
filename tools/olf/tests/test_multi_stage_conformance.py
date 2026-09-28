from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _contract_conformance import (
    ACTIVATION_URIS,
    CAPABILITY_GATED_EXPORTS,
    MEDALLION_LAYERS,
    STAGE_BINDINGS,
    STAGE_DERIVED_EXPORTS,
    bucket,
    logical_identities,
    names_stage,
)
from conftest import CONFORMANCE_PROFILE, CONFORMANCE_STAGES, E2E_REPO_ROOT

from olf.commands._shared import deployment_context_for_profile
from olf.contracts import build_contract_env
from olf.contracts_check import profile_schema_errors
from olf.deployment.context import DeploymentContext, Provider, stage_namespace, topology_variables
from olf.profile import DeploymentTopology, Preset, StageName, load_deployment_profile, resolve_topology
from olf.provider_contracts import parse_provider_contracts

# A real captured local DEV+PROD provider contract -- the exact payload
# `terraform output -json provider_contracts` gives olf once the local
# platform root is applied, per `test_provider_conformance.py`. There is no
# way to produce one without a live kind cluster, which this slice does not
# have (see the module docstring in that file); `deployment.profile_name` is
# the only field rebound below, to the conformance profile actually under
# test, because `parse_provider_contracts` requires it to match the resolved
# topology it is handed. Every stage identity in it -- namespace, catalog,
# buckets, principal, activation prefix -- is untouched captured output.
_LOCAL_CONTRACT_FIXTURE = Path(__file__).parent / "fixtures" / "local-provider-contracts-v3.json"


def _local_contract_for(topology: DeploymentTopology) -> dict[str, Any]:
    contract = json.loads(_LOCAL_CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    contract["deployment"]["profile_name"] = topology.profile_name
    return contract


def test_conformance_profile_resolves_a_non_vacuous_local_baseline() -> None:
    assert profile_schema_errors(E2E_REPO_ROOT, profile_path=CONFORMANCE_PROFILE) == []

    topology = resolve_topology(load_deployment_profile(CONFORMANCE_PROFILE))
    enabled = tuple(stage.name for stage in topology.stages if stage.enabled)

    assert topology.provider == Provider.LOCAL
    # 'full', so the nightly that applies this file can run the Superset and
    # OpenMetadata assertions in both stages (#220).
    assert topology.preset == Preset.FULL
    assert enabled == CONFORMANCE_STAGES
    assert all(
        stage.capabilities.analytics and stage.capabilities.governance for stage in topology.stages if stage.enabled
    )


@pytest.mark.parametrize("stage", CONFORMANCE_STAGES, ids=[stage.value for stage in CONFORMANCE_STAGES])
def test_selecting_a_stage_preserves_the_two_stage_topology(stage: StageName) -> None:
    context: DeploymentContext = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=stage.value)
    other = next(item for item in CONFORMANCE_STAGES if item != stage)
    stages = json.loads(topology_variables(context)["stages"])

    assert context.stage == stage
    assert context.namespace == stage_namespace(stage) != stage_namespace(other)
    assert tuple(name for name, value in sorted(stages.items()) if value["enabled"]) == tuple(
        item.value for item in CONFORMANCE_STAGES
    )


def _parsed_conformance_contract():  # noqa: ANN202 - olf.provider_contracts.ProviderContracts
    """The generated local runtime configuration for both of the conformance
    profile's stages, parsed against the topology `openlakeforge.conformance.yaml`
    itself resolves to (#155's negative-isolation slice).

    `stage` is irrelevant to `DeploymentContext.topology` -- `deployment_context_for_profile`
    resolves the whole profile regardless of which stage is selected -- so DEV's
    context is as good a source for it as PROD's.
    """
    topology = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value).topology
    return parse_provider_contracts(_local_contract_for(topology), topology)


@pytest.mark.parametrize("stage", CONFORMANCE_STAGES, ids=[stage.value for stage in CONFORMANCE_STAGES])
def test_stage_runtime_configuration_matches_its_own_context_and_canonical_identities(stage: StageName) -> None:
    """The namespace, catalog, and activation prefix the generated runtime
    configuration binds one stage to are exactly the ones `DeploymentContext`
    itself resolves for that stage from the real conformance profile, and the
    canonical, provider-neutral identity every stage derives from its name
    alone (AGENTS.md rule 2)."""
    context = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=stage.value)
    stage_contract = _parsed_conformance_contract().for_stage(stage)

    assert stage_contract.namespace == context.namespace
    for field, actual, canonical in logical_identities(stage, stage_contract):
        assert actual == canonical, (
            f"the conformance profile's {stage.value!r} stage names its {field} {actual!r}, not the canonical "
            f"{canonical!r} shared code derives from the stage alone."
        )


def test_stage_identities_do_not_collide_between_dev_and_prod() -> None:
    """Acceptance criterion: "stage catalog/storage identities do not
    collide." Each stage's namespace, catalog, runtime principal, and
    per-layer bucket is its own -- no value generated for one stage is ever
    reused for the other."""
    parsed = _parsed_conformance_contract()

    # Compared per resource domain, not globally: `provider_contracts.py`
    # tracks namespaces, catalogs, principals and storage independently, so a
    # stage whose namespace and principal happen to share a spelling is
    # legitimate. Collapsing them into one map would fail that deployment
    # while proving nothing about cross-stage isolation. Storage keeps its
    # layers together, so two layers sharing one bucket is still a collision.
    seen: dict[str, dict[str, str]] = {}
    for stage in CONFORMANCE_STAGES:
        stage_contract = parsed.for_stage(stage)
        identities = {
            "namespace": ("namespace", stage_contract.namespace),
            "catalog": ("catalog", str(stage_contract.catalog["catalog_name"])),
            "runtime_identity.principal": ("principal", str(stage_contract.runtime_identity["principal"])),
            **{f"storage.{layer}": ("storage", bucket(stage_contract, layer)) for layer in MEDALLION_LAYERS},
        }
        for kind, (domain, value) in identities.items():
            owner = f"{stage.value}/{kind}"
            within = seen.setdefault(domain, {})
            assert value not in within, (
                f"{owner} shares its {domain} identity ({value!r}) with {within[value]}. Stage isolation "
                f"depends on every one of these being unique to its own stage."
            )
            within[value] = owner


def test_stage_generated_configuration_addresses_only_that_stage() -> None:
    """Acceptance criteria: "DEV-generated runtime configuration cannot
    read/write PROD" and "PROD-generated runtime configuration cannot write
    DEV." The runtime environment `olf` builds for one stage binds every
    contracted variable to that stage's own value, keeps every activation
    artifact under that stage's own prefix, and carries no export naming the
    sibling stage -- reusing exactly the classification
    `test_provider_conformance.py` proves this shape with.

    Scope: the *topology* comes from the conformance profile, but the stage
    bindings come from the captured `acme-data` contract, because no cluster
    exists here to render a contract from this profile. So this proves the
    isolation logic over a real two-stage contract. That applying the
    conformance profile yields isolated bindings is proven where a cluster
    does exist: the multi-stage nightly applies this exact file and runs the
    cross-stage isolation probes against the contract it renders (#220).
    """
    topology = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value).topology
    contract = _local_contract_for(topology)
    parsed = parse_provider_contracts(contract, topology)
    per_stage = {
        stage: build_contract_env({}, contract, repo_root=E2E_REPO_ROOT, topology=topology, stage=stage)[0]
        for stage in CONFORMANCE_STAGES
    }

    for stage in CONFORMANCE_STAGES:
        exports = per_stage[stage]
        stage_contract = parsed.for_stage(stage)
        # Absence is a mismatch, not a pass: an export dropped from generation
        # entirely would otherwise be skipped here, and skipped again by the
        # varying-key check below when it is missing for both stages.
        wrong = {
            key: (exports.get(key), str(expected(stage_contract)))
            for key, expected in STAGE_BINDINGS
            if exports.get(key) != str(expected(stage_contract))
        }
        assert not wrong, (
            f"the conformance profile's {stage.value!r} environment binds {wrong!r} as (emitted, contracted), "
            f"where a None emitted value means the export is missing altogether. Every one of these is how a "
            f"run reaches data or authenticates to Trino, so a binding that is not this stage's own is one "
            f"stage reading or writing another's, and a binding that is absent is one the runtime never gets."
        )

        prefix = str(stage_contract.activation["prefix"])
        astray = {
            key: exports.get(key)
            for key, suffix in ACTIVATION_URIS.items()
            if not (exports.get(key) or "").endswith(f"/{prefix}{suffix}")
        }
        assert not astray, (
            f"the conformance profile's {stage.value!r} activation artifacts land at {astray!r}, outside its "
            f"contracted {prefix!r} prefix, where None means the export is missing altogether. Stages share "
            f"the ops bucket; the prefix is the only thing keeping one stage's manifests, logs and run "
            f"artifacts out of another's."
        )

        others = {other.value for other in CONFORMANCE_STAGES if other != stage}
        leaked = sorted(
            (key, other) for key, value in exports.items() for other in others if names_stage(value, other)
        )
        assert not leaked, (
            f"the conformance profile's {stage.value!r} environment carries {leaked!r} as (export, stage "
            f"named). A command or rollout handed another stage's catalog, bucket or principal reads, writes or "
            f"authenticates as that stage."
        )

    varying = {
        key
        for key in set().union(*(set(exports) for exports in per_stage.values()))
        if len({exports.get(key) for exports in per_stage.values()}) > 1
    }
    unclassified = sorted(
        varying - {key for key, _ in STAGE_BINDINGS} - set(ACTIVATION_URIS) - STAGE_DERIVED_EXPORTS
        - CAPABILITY_GATED_EXPORTS
    )
    assert not unclassified, (
        f"the conformance profile emits {unclassified!r} with a different value per stage, and nothing in this "
        f"module classifies them. Add each to STAGE_BINDINGS, STAGE_DERIVED_EXPORTS, or CAPABILITY_GATED_EXPORTS "
        f"in _contract_conformance.py -- leaving it here means a stage-carrying value nobody asserts."
    )
