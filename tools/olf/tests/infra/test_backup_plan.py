"""What `olf infra backup` holds is derived from the applied provider contract (#910)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from _backup_support import full_contracts

from olf.common.provider_contracts import ProviderContracts
from olf.infra.backup import BackupError
from olf.infra.backup._plan import SUPERSET_USER_SOURCE_PREFIX, SUPERSET_USER_STAGE_SECRET, plan_from_contract


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
        *(f"olf-conformance-{stage}-{layer}" for stage in ("dev", "prod") for layer in ("bronze", "silver", "gold")),
        "olf-conformance-ops",
    )


def test_a_full_contract_plans_the_data_bound_secrets_and_the_local_ca() -> None:
    plan = plan_from_contract(full_contracts(), shared_namespace="olf-system", github_oauth_secret="github-oauth-app")

    assert {(ref.namespace, ref.name) for ref in plan.secrets} == {
        ("olf-system", "polaris-bootstrap-credentials"),
        ("olf-system", "polaris-om-creds"),
        # Beside each stage's own Trino (#1021), which reads them through a namespace-local secretKeyRef.
        ("olf-dev", "polaris-dev-trino-catalog-creds"),
        ("olf-prod", "polaris-prod-trino-catalog-creds"),
        ("olf-dev", "polaris-dev-processing-creds"),
        ("olf-dev", "polaris-dev-deployer-creds"),
        ("olf-prod", "polaris-prod-processing-creds"),
        ("olf-prod", "polaris-prod-deployer-creds"),
        ("olf-dev", "superset-credentials"),
        ("olf-prod", "superset-credentials"),
        ("olf-system", "platform-api-superset-dev"),
        ("olf-system", "platform-api-superset-prod"),
        ("olf-dev", "platform-api-superset"),
        ("olf-prod", "platform-api-superset"),
        ("olf-system", "openmetadata-fernet-key"),
        ("olf-system", "openmetadata-admin"),
        ("olf-system", "openmetadata-ingestion-bot"),
        ("olf-system", "openmetadata-automation-bot"),
        ("olf-system", "github-oauth-app"),
        ("olf-system", "local-ca"),
    }


def test_the_superset_service_user_secret_names_are_the_ones_terraform_creates() -> None:
    tf = (
        Path(__file__).resolve().parents[4] / "infra/terraform/modules/access/platform-api/superset_users.tf"
    ).read_text()

    assert f'superset_stage_secret = "{SUPERSET_USER_STAGE_SECRET}"' in tf
    assert f'"{SUPERSET_USER_SOURCE_PREFIX}${{stage}}"' in tf


def test_a_stage_without_analytics_or_governance_or_dex_is_not_planned() -> None:
    plan = plan_from_contract(_slim(full_contracts()), shared_namespace="olf-system")

    assert [name for name, _ in plan.databases] == ["polaris", "dagster_dev", "dagster_prod"]
    assert {ref.name for ref in plan.secrets}.isdisjoint(
        {"superset-credentials", "openmetadata-fernet-key", "github-oauth-app", "local-ca", "openmetadata-admin"}
    )
    assert not any(ref.name.startswith("platform-api-superset") for ref in plan.secrets)
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


def test_trino_tls_alone_does_not_back_up_the_disposable_ca() -> None:
    plan = plan_from_contract(_port_forward(full_contracts(), trino_tls=True), shared_namespace="olf-system")
    assert "local-ca" not in {secret.name for secret in plan.secrets}


def test_plain_http_routes_do_not_back_up_the_ca_and_https_routes_do() -> None:
    contracts = full_contracts()
    for issuer, backed_up in (("none", False), ("local-ca", True)):
        access = {**contracts.shared.values["access"], "tls": issuer}
        shared = replace(contracts.shared, values={**contracts.shared.values, "access": access})
        plan = plan_from_contract(replace(contracts, shared=shared), shared_namespace="olf-system")
        assert ("local-ca" in {secret.name for secret in plan.secrets}) is backed_up, issuer
