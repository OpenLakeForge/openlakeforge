"""What `olf infra backup` holds is derived from the applied provider contract (#910)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from _backup_support import full_contracts

from olf.common.provider_contracts import ProviderContracts
from olf.infra.backup import BackupError
from olf.infra.backup._plan import plan_from_contract


def _slim(contracts: ProviderContracts) -> ProviderContracts:
    # Without access, Trino workload authentication is off, so no query binding carries a CA ConfigMap.
    no_tls = {"tls_port", "ca_configmap_name"}
    stages = {
        name: replace(
            stage, reporting=None, governance=None, query={k: v for k, v in stage.query.items() if k not in no_tls}
        )
        for name, stage in contracts.stages.items()
    }
    values = {key: value for key, value in contracts.shared.values.items() if key not in {"access", "identity"}}
    return replace(contracts, stages=stages, shared=replace(contracts.shared, values=values))


def test_a_full_dev_prod_contract_plans_exactly_the_seven_databases_and_the_buckets() -> None:
    plan = plan_from_contract(full_contracts(), shared_namespace="olf-system", github_oauth_secret="github-oauth-app")

    assert plan.databases == (
        ("polaris", "polaris"),
        ("dagster_dev", "dagster_dev"),
        ("dagster_prod", "dagster_prod"),
        ("superset_dev", "superset_dev"),
        ("superset_prod", "superset_prod"),
        ("openmetadata_db", "openmetadata_user"),
        ("dex", "dex"),
    )
    assert plan.buckets == (
        *(f"lakehouse-{stage}-{layer}" for stage in ("dev", "prod") for layer in ("bronze", "silver", "gold")),
        "openlakeforge-ops",
    )


def test_a_full_contract_plans_the_data_bound_secrets_and_the_local_ca() -> None:
    plan = plan_from_contract(full_contracts(), shared_namespace="olf-system", github_oauth_secret="github-oauth-app")

    assert {(ref.namespace, ref.name) for ref in plan.secrets} == {
        ("olf-system", "polaris-bootstrap-credentials"),
        ("olf-system", "polaris-om-creds"),
        ("olf-system", "polaris-dev-trino-creds"),
        ("olf-system", "polaris-prod-trino-creds"),
        ("olf-system", "polaris-dev-trino-people-creds"),
        ("olf-system", "polaris-prod-trino-people-creds"),
        ("olf-dev", "polaris-dev-floe-creds"),
        ("olf-dev", "polaris-dev-deployer-creds"),
        ("olf-prod", "polaris-prod-floe-creds"),
        ("olf-prod", "polaris-prod-deployer-creds"),
        ("olf-dev", "superset-credentials"),
        ("olf-prod", "superset-credentials"),
        ("olf-system", "openmetadata-fernet-key"),
        ("olf-system", "openmetadata-admin"),
        ("olf-system", "openmetadata-ingestion-bot"),
        ("olf-system", "openmetadata-automation-bot"),
        ("olf-system", "github-oauth-app"),
        ("olf-system", "local-ca"),
    }


def test_a_stage_without_analytics_or_governance_or_dex_is_not_planned() -> None:
    plan = plan_from_contract(_slim(full_contracts()), shared_namespace="olf-system")

    assert [name for name, _ in plan.databases] == ["polaris", "dagster_dev", "dagster_prod"]
    assert {ref.name for ref in plan.secrets}.isdisjoint(
        {"superset-credentials", "openmetadata-fernet-key", "github-oauth-app", "local-ca", "openmetadata-admin"}
    )
    assert ("olf-system", "polaris-bootstrap-credentials") in {(ref.namespace, ref.name) for ref in plan.secrets}


def test_a_cloud_deployment_is_refused_rather_than_planned_with_local_names() -> None:
    contracts = full_contracts()
    aws = replace(contracts, deployment={**contracts.deployment, "provider": "aws"})
    with pytest.raises(BackupError, match="local provider only"):
        plan_from_contract(aws, shared_namespace="olf-system")


def _port_forward(contracts: ProviderContracts, *, trino_tls: bool) -> ProviderContracts:
    access = {**contracts.shared.values["access"], "implementation": "access.port_forward"}
    shared = replace(contracts.shared, values={**contracts.shared.values, "access": access})
    stages = {
        name: replace(
            stage,
            query={k: v for k, v in stage.query.items() if trino_tls or k not in {"tls_port", "ca_configmap_name"}},
        )
        for name, stage in contracts.stages.items()
    }
    return replace(contracts, shared=shared, stages=stages)


def test_a_port_forward_deployment_without_trino_tls_plans_no_local_ca() -> None:
    plan = plan_from_contract(_port_forward(full_contracts(), trino_tls=False), shared_namespace="olf-system")
    assert "local-ca" not in {secret.name for secret in plan.secrets}


def test_trino_tls_alone_still_plans_the_local_ca() -> None:
    plan = plan_from_contract(_port_forward(full_contracts(), trino_tls=True), shared_namespace="olf-system")
    assert "local-ca" in {secret.name for secret in plan.secrets}
