"""The platform phase bootstraps OpenMetadata from the applied provider contract (#562)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import CONFORMANCE_PROFILE

from olf.common.deployment.errors import DeploymentPreconditionError
from olf.common.profile import StageName
from olf.common.provider_contracts import ProviderContracts, parse_provider_contracts
from olf.infra.commands._shared import deployment_context_for_profile
from olf.infra.commands.openmetadata import bootstrap_from_contract
from olf.infra.deployment.engine import DeploymentEngine, DeploymentPhase
from olf.infra.openmetadata import _platform
from olf.infra.openmetadata._catalog_seed import CatalogSeed
from olf.infra.openmetadata._stage_services import StageServices

# Captured from the local root applied with openlakeforge.conformance.yaml: DEV and PROD, both
# governed and with analytics, Trino on TLS.
_CONTRACT = json.loads((Path(__file__).parents[1] / "fixtures" / "conformance-provider-contracts.json").read_text())


class _Provider:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.context = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value)
        self.env = self.context.command_env()

    def platform_up(self) -> None:
        self.calls.append("platform_up")


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The applied contract and the cluster edges; records what the bootstrap was given."""
    seen: dict[str, Any] = {"configmap": {"config.json": "{}"}}

    @contextmanager
    def port_forward(service: str, remote_port: int, namespace: str, **_: Any) -> Iterator[int]:
        seen["forwarded"] = (service, remote_port, namespace)
        yield 18585

    def run_bootstrap(route: Any, namespace: str, **kwargs: Any) -> None:
        seen["calls"].append("bootstrap")
        seen["bootstrap"] = {"url": route.url, "namespace": namespace, **kwargs}

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda *_a, **_k: _CONTRACT)
    monkeypatch.setattr("olf.infra.k8s.configmap_data", lambda *_a, **_k: seen["configmap"])
    monkeypatch.setattr("olf.infra.k8s.port_forward", port_forward)
    monkeypatch.setattr(_platform, "run_bootstrap", run_bootstrap)
    return seen


def test_the_platform_phase_bootstraps_openmetadata_after_the_apply_from_the_contract(cluster: dict) -> None:
    cluster["calls"] = []
    provider = _Provider(cluster["calls"])

    DeploymentEngine(provider).deploy(DeploymentPhase.PLATFORM)

    assert cluster["calls"] == ["platform_up", "bootstrap"]
    assert cluster["forwarded"] == ("openmetadata", 8585, "olf-system")
    given = cluster["bootstrap"]
    assert (given["url"], given["namespace"]) == ("http://127.0.0.1:18585", "olf-system")
    assert (given["email"], given["sso"]) == ("admin@open-metadata.org", True)
    assert given["seed"] == CatalogSeed(
        service="polaris",
        display_name="Lakehouse catalogs (Trino)",
        trino_namespace="trino://trino.olf-system:8080",
        trino_tls_namespace="trino://trino.olf-system:8443",
        pipeline_service="dagster_dev",
        databases=("lakehouse_dev", "lakehouse_prod"),
    )
    assert given["stages"] == (
        StageServices("dev", "http://dagster-dagster-webserver.olf-dev:80", "http://superset.olf-dev:8088", "olf-dev"),
        StageServices(
            "prod", "http://dagster-dagster-webserver.olf-prod:80", "http://superset.olf-prod:8088", "olf-prod"
        ),
    )
    assert given["environ"]["KUBECONFIG"] == provider.env["KUBECONFIG"]


def test_basic_login_is_kept_without_the_published_sso_configuration(cluster: dict) -> None:
    cluster.update(calls=[], configmap=None)

    DeploymentEngine(_Provider(cluster["calls"])).deploy(DeploymentPhase.PLATFORM)

    assert cluster["bootstrap"]["sso"] is False


def _parsed() -> ProviderContracts:
    topology = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value).topology
    return parse_provider_contracts(_CONTRACT, topology)


def _with(contracts: ProviderContracts, **changes: dict[str, Any]) -> ProviderContracts:
    stages = {name: replace(stage, **changes.get(name.value, {})) for name, stage in contracts.stages.items()}
    return replace(contracts, stages=stages)


def test_a_stage_without_governance_is_left_out_and_the_next_one_is_canonical() -> None:
    contracts = _with(_parsed(), dev={"governance": None}, prod={"reporting": None})

    inputs = _platform.bootstrap_inputs(contracts)

    assert inputs is not None
    seed, stages = inputs
    assert (seed.pipeline_service, seed.databases) == ("dagster_prod", ("lakehouse_prod",))
    assert stages == (StageServices("prod", "http://dagster-dagster-webserver.olf-prod:80"),)


def test_nothing_is_bootstrapped_when_no_stage_is_governed(cluster: dict) -> None:
    cluster["calls"] = []
    contracts = _with(_parsed(), dev={"governance": None}, prod={"governance": None})

    bootstrap_from_contract(contracts, environ={})

    assert cluster["calls"] == [] and "forwarded" not in cluster


def test_the_platform_phase_fails_when_the_apply_left_no_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda *_a, **_k: None)
    calls: list[str] = []

    with pytest.raises(DeploymentPreconditionError, match="provider contracts are unavailable"):
        DeploymentEngine(_Provider(calls)).deploy(DeploymentPhase.PLATFORM)

    assert calls == ["platform_up"]
