"""olf's Polaris bootstrap: catalogs, principal credentials, grants and people's roles (#562)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from _polaris_fake import ASSIGNED, ROOT, STAGES, STS, Cluster, FakePolaris, conformance_plan, serve
from conftest import CONFORMANCE_PROFILE

from olf.common.profile import StageName
from olf.common.provider_contracts import ProviderContracts, parse_provider_contracts
from olf.common.tooling.kubectl import Kubectl
from olf.infra import k8s
from olf.infra.commands._shared import deployment_context_for_profile
from olf.infra.polaris import PolarisClient, PolarisConfig, PolarisError
from olf.infra.polaris_bootstrap import (
    DROP_WITH_PURGE,
    BootstrapPlan,
    CatalogSts,
    Credential,
    PersonAccess,
    StageCatalog,
    bootstrap_from_contract,
    bootstrap_plan,
    ensure_catalog,
    ensure_credential,
    run_bootstrap,
)

_ENVIRON = {"KUBE_CONTEXT": "kind-test"}
_SHARED = Credential("trino-dev", "polaris-dev-trino-creds", "TRINO_ID", "TRINO_SECRET", "olf-system")
# Stage namespaces are deleted by `--allow-stage-removal` and recreated empty.
_STAGE = Credential("floe-dev", "polaris-dev-floe-creds", "FLOE_ID", "FLOE_SECRET", "olf-dev", recreatable=True)
_DEV = StageCatalog(
    "dev",
    "lakehouse_dev",
    "lakehouse-dev-bronze",
    "lakehouse-dev-silver",
    "lakehouse-dev-gold",
    _SHARED,
    _STAGE,
    _STAGE,
)


@pytest.fixture
def polaris() -> Iterator[tuple[FakePolaris, PolarisClient]]:
    fake = FakePolaris()
    with serve(fake) as url:
        client = PolarisClient(PolarisConfig(url, "", *ROOT))
        client.login()
        yield fake, client


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Cluster:
    cluster = Cluster(tmp_path / "cluster.json")
    resolver = Mock()
    resolver.resolve.return_value = tmp_path / "kubectl"
    monkeypatch.setattr(k8s, "ProcessRunner", lambda: cluster)
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda *_a: "kubectl")
    monkeypatch.setattr(k8s.Toolkit, "default", lambda **_k: SimpleNamespace(kubectl=Kubectl(cluster, resolver)))
    return cluster


def _ensure(client: PolarisClient, credential: Credential = _SHARED) -> None:
    ensure_credential(client, credential, environ=_ENVIRON)


def _current(credential: Credential) -> dict[str, dict[str, str]]:
    key = f"secret/{credential.namespace}/{credential.secret}"
    return {
        key: {
            credential.client_id_key: f"id of {credential.principal}",
            credential.client_secret_key: f"secret of {credential.principal}",
        }
    }


@pytest.mark.parametrize("sts", [CatalogSts(**STS), None], ids=["sts", "static-keys"])
def test_a_new_stage_catalog_is_confined_to_its_buckets_and_allows_drop_with_purge(
    polaris: tuple[FakePolaris, PolarisClient], sts: CatalogSts | None
) -> None:
    fake, client = polaris

    ensure_catalog(client, _DEV, sts)

    storage: dict[str, Any] = {
        "storageType": "S3",
        "allowedLocations": [f"s3://lakehouse-dev-{layer}/" for layer in ("bronze", "silver", "gold")],
        "pathStyleAccess": True,
    }
    if sts:
        storage |= {
            "roleArn": sts.role_arn,
            "region": sts.region,
            "endpoint": sts.endpoint,
            "stsEndpoint": sts.sts_endpoint,
            "stsUnavailable": False,
        }
    else:
        storage["stsUnavailable"] = True
    assert fake.state()["catalogs"] == {
        "lakehouse_dev": {
            "name": "lakehouse_dev",
            "type": "INTERNAL",
            "properties": {"default-base-location": "s3://lakehouse-dev-silver/", DROP_WITH_PURGE: "true"},
            "storageConfigInfo": storage,
        }
    }


def test_an_existing_catalog_gains_drop_with_purge_and_keeps_its_other_properties(
    polaris: tuple[FakePolaris, PolarisClient], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, client = polaris
    before = {"default-base-location": "s3://lakehouse-dev-silver/", "operator-set": "kept"}
    fake.catalogs["lakehouse_dev"] = {
        "name": "lakehouse_dev",
        "properties": dict(before),
        "storageConfigInfo": {"storageType": "S3", "stsUnavailable": True},
        "entityVersion": 3,
    }

    ensure_catalog(client, _DEV, CatalogSts(**STS))

    assert fake.catalogs["lakehouse_dev"]["properties"] == {**before, DROP_WITH_PURGE: "true"}
    # Polaris will not add a role ARN to an existing catalog, so the operator is told.
    assert "Catalog lakehouse_dev was created without STS" in capsys.readouterr().err


def test_a_catalog_that_allows_purge_is_not_updated(polaris: tuple[FakePolaris, PolarisClient]) -> None:
    fake, client = polaris
    ensure_catalog(client, _DEV, None)
    fake.writes.clear()

    ensure_catalog(client, _DEV, None)

    assert fake.writes == ["POST /api/management/v1/catalogs"]  # answered 409


def test_a_new_principal_gets_its_secret_and_no_marker_is_left(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris

    _ensure(client)

    assert cluster.state(fake) == _current(_SHARED)


def test_a_rerun_rotates_nothing_and_writes_nothing(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris
    _ensure(client)
    before, issued = cluster.load(), fake.issued
    fake.writes.clear()
    cluster.argv.clear()

    _ensure(client)

    assert (cluster.load(), fake.issued, fake.writes) == (before, issued, [])
    assert {argv[0] for argv in cluster.argv} == {"get"}


def test_a_pending_marker_rotates_the_principal_its_secret_was_lost_for(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris
    _ensure(client)
    # The window the marker exists for: Polaris created the principal, Kubernetes kept an older Secret.
    cluster.put("configmap", "olf-system", "polaris-dev-trino-creds-bootstrap-pending", {"principal": "trino-dev"})
    cluster.put("secret", "olf-system", "polaris-dev-trino-creds", {"TRINO_ID": "old", "TRINO_SECRET": "old"})

    _ensure(client)

    assert cluster.state(fake) == _current(_SHARED)


def test_a_re_enabled_stage_namespace_gets_fresh_credentials(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, client = polaris
    _ensure(client, _STAGE)
    cluster.save({})  # the namespace was deleted; the principal survives in the metastore

    _ensure(client, _STAGE)

    assert cluster.state(fake) == _current(_STAGE)
    assert "Polaris principal 'floe-dev' exists but 'olf-dev' has no Secret" in capsys.readouterr().err


def test_a_shared_secret_deleted_by_hand_fails_without_rotating_the_client_using_it(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris
    _ensure(client)
    issued = fake.issued
    cluster.save({})

    with pytest.raises(PolarisError, match="'trino-dev' exists but credential Secret 'polaris-dev-trino-creds'"):
        _ensure(client)

    assert fake.issued == issued


def test_a_credential_polaris_did_not_return_fails_and_keeps_the_marker(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, client = polaris
    monkeypatch.setattr(fake, "_issue", lambda name: {"principal": {"name": name}})

    with pytest.raises(PolarisError, match="no client credentials for principal 'trino-dev'"):
        _ensure(client)

    assert cluster.load() == {
        "configmap/olf-system/polaris-dev-trino-creds-bootstrap-pending": {"principal": "trino-dev"}
    }


def test_the_rerun_after_a_lost_credential_rotates_it(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris
    fake.principals["trino-dev"] = {}
    cluster.put("configmap", "olf-system", "polaris-dev-trino-creds-bootstrap-pending", {"principal": "trino-dev"})

    _ensure(client)

    assert cluster.state(fake) == _current(_SHARED)


def test_no_credential_reaches_argv_or_output(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, client = polaris
    _ensure(client)
    cluster.put("configmap", "olf-system", "polaris-dev-trino-creds-bootstrap-pending", {"principal": "trino-dev"})
    _ensure(client)

    output = capsys.readouterr()
    exposed = output.out + output.err + json.dumps(cluster.argv)
    assert fake.principals["trino-dev"]["clientSecret"] not in exposed
    assert ROOT[1] not in exposed
    assert "Stored the credentials of Polaris principal 'trino-dev' in 'olf-system'." in output.out


def test_an_unexpected_polaris_answer_fails(
    polaris: tuple[FakePolaris, PolarisClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, client = polaris
    monkeypatch.setattr(fake, "handle", lambda *_a: (503, {}))

    with pytest.raises(PolarisError, match="HTTP 503") as raised:
        ensure_catalog(client, _DEV, None)

    assert raised.value.status == 503


def _bootstrap(client: PolarisClient, plan: BootstrapPlan | None = None) -> None:
    run_bootstrap(client, plan or conformance_plan(), environ=_ENVIRON)


def _secret(namespace: str, name: str, keys: str, principal: str) -> dict[str, dict[str, str]]:
    return _current(Credential(principal, name, f"{keys}_CLIENT_ID", f"{keys}_CLIENT_SECRET", namespace))


def test_a_fresh_stack_gets_every_principal_role_grant_and_secret(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris

    _bootstrap(client)

    state = fake.state()
    assert sorted(state["catalogs"]) == ["lakehouse_dev", "lakehouse_prod"]
    assert state["principals"] == {
        **{f"trino-{s}": [f"data-engineer-{s}"] for s in STAGES},
        **{f"floe-{s}": [f"data-writer-{s}"] for s in STAGES},
        **{f"deployer-{s}": [f"namespace-admin-{s}"] for s in STAGES},
        "openmetadata": ["data-reader"],
        "github": sorted(ASSIGNED),
        **{f"trino-people-{s}": [] for s in STAGES},
    }
    assert state["catalog_roles"]["lakehouse_dev"] == {
        "catalog-admin": ["CATALOG_MANAGE_CONTENT"],
        "catalog-writer": ["CATALOG_MANAGE_CONTENT"],
        "catalog-reader-dev": ["CATALOG_MANAGE_CONTENT"],
        "catalog-namespace-admin": ["CATALOG_MANAGE_ACCESS", "CATALOG_MANAGE_METADATA"],
        "olf-write": ["CATALOG_MANAGE_CONTENT"],
        "olf-read": ["NAMESPACE_READ_PROPERTIES", "TABLE_READ_DATA", "VIEW_READ_PROPERTIES"],
        "olf-gold-read": ["NAMESPACE_LIST"],
    }
    assert state["principal_roles"] == {
        "data-engineer-dev": {"lakehouse_dev": ["catalog-admin"]},
        "data-engineer-prod": {"lakehouse_prod": ["catalog-admin"]},
        "data-writer-dev": {"lakehouse_dev": ["catalog-writer"]},
        "data-writer-prod": {"lakehouse_prod": ["catalog-writer"]},
        "data-reader": {"lakehouse_dev": ["catalog-reader-dev"], "lakehouse_prod": ["catalog-reader-prod"]},
        "namespace-admin-dev": {"lakehouse_dev": ["catalog-namespace-admin"]},
        "namespace-admin-prod": {"lakehouse_prod": ["catalog-namespace-admin"]},
        **{role: {catalog: [name] for catalog, name in catalogs.items()} for role, catalogs in ASSIGNED.items()},
    }
    expected: dict[str, dict[str, str]] = {}
    for s in STAGES:
        expected |= _secret("olf-system", f"polaris-{s}-trino-creds", "POLARIS_TRINO", f"trino-{s}")
        expected |= _secret(f"olf-{s}", f"polaris-{s}-floe-creds", "POLARIS_FLOE", f"floe-{s}")
        expected |= _secret(f"olf-{s}", f"polaris-{s}-deployer-creds", "POLARIS_DEPLOYER", f"deployer-{s}")
        expected |= _secret("olf-system", f"polaris-{s}-trino-people-creds", "POLARIS", f"trino-people-{s}")
    expected |= _secret("olf-system", "polaris-om-creds", "POLARIS_OM", "openmetadata")
    assert cluster.state(fake) == expected


def test_a_rerun_of_the_whole_bootstrap_changes_nothing(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris
    _bootstrap(client)
    before, issued = (fake.state(), cluster.state(fake)), fake.issued
    fake.writes.clear()
    cluster.argv.clear()

    _bootstrap(client)

    assert (fake.state(), cluster.state(fake), fake.issued) == (*before, issued)
    # Only creates Polaris answers with 409, and the legacy dbt principal's removal.
    assert [w for w in fake.writes if not w.startswith("POST")] == ["DELETE /api/management/v1/principals/dbt"]
    assert [argv for argv in cluster.argv if argv[0] in {"create", "apply"}] == []
    assert {argv[2] for argv in cluster.argv if argv[0] == "delete"} == {"polaris-dbt-creds"}


def test_the_legacy_dbt_principal_and_its_secret_are_removed(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris
    fake.principals["dbt"] = {"roles": []}
    cluster.put("secret", "olf-system", "polaris-dbt-creds", {"POLARIS_DBT_CLIENT_ID": "x"})

    _bootstrap(client)

    assert "dbt" not in fake.principals
    assert "secret/olf-system/polaris-dbt-creds" not in cluster.load()


def test_without_people_the_deployer_keeps_its_narrower_privilege(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris

    _bootstrap(client, conformance_plan(person_access=False))

    assert fake.state()["catalog_roles"]["lakehouse_dev"]["catalog-namespace-admin"] == ["CATALOG_MANAGE_METADATA"]
    assert "github" not in fake.principals
    assert not any(name.startswith("olf-") for name in fake.principal_roles)


def test_a_narrowed_data_access_row_loses_the_grant_it_no_longer_names(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster
) -> None:
    fake, client = polaris
    plan = conformance_plan()
    assert plan.person_access is not None
    wider = {**ASSIGNED, "olf-analyst": {"lakehouse_dev": "olf-read", "lakehouse_prod": "olf-read"}}
    _bootstrap(client, replace(plan, person_access=PersonAccess("github", wider, plan.person_access.people)))
    fake.principal_roles["olf-signed-in"]["lakehouse_dev"] = ["olf-write"]

    _bootstrap(client, plan)

    gold = ["olf-gold-read"]
    assert fake.state()["principal_roles"]["olf-analyst"] == {"lakehouse_dev": gold, "lakehouse_prod": gold}
    assert fake.state()["principal_roles"]["olf-signed-in"] == {}


# Captured from the local root applied with openlakeforge.conformance.yaml: DEV and PROD, GitHub sign-in.
_CONTRACT = json.loads((Path(__file__).parents[1] / "fixtures" / "conformance-provider-contracts.json").read_text())
_GLUE = {"catalog_provider": "aws-glue", "catalog_name": "lakehouse"}


def _contracts(**stage_changes: dict[str, Any]) -> ProviderContracts:
    topology = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value).topology
    contracts = parse_provider_contracts(_CONTRACT, topology)
    stages = {name: replace(stage, **stage_changes.get(name.value, {})) for name, stage in contracts.stages.items()}
    return replace(contracts, stages=stages)


def test_the_plan_from_the_contract_is_what_the_job_is_rendered_with() -> None:
    # conformance_plan() holds the values the local root hands bootstrap.sh.tftpl for this contract.
    assert bootstrap_plan(_contracts()) == conformance_plan()


def test_people_get_no_access_without_github_sign_in() -> None:
    contracts = _contracts()
    identity = {k: v for k, v in contracts.shared.values["identity"].items() if k != "adapter"}
    shared = replace(contracts.shared, values={**contracts.shared.values, "identity": identity})

    plan = bootstrap_plan(replace(contracts, shared=shared))

    assert plan is not None and plan.person_access is None


def test_a_stage_on_glue_is_left_out() -> None:
    dev_only = bootstrap_plan(_contracts(prod={"catalog": _GLUE}))

    assert dev_only is not None and [stage.stage for stage in dev_only.stages] == ["dev"]
    assert bootstrap_plan(_contracts(dev={"catalog": _GLUE}, prod={"catalog": _GLUE})) is None


@pytest.fixture
def forwarded(polaris: tuple[FakePolaris, PolarisClient], monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Every port-forward and root Secret read `bootstrap_from_contract` makes, answered by the fake Polaris."""
    _, client = polaris
    calls: list[tuple[str, ...]] = []

    @contextmanager
    def port_forward(service: str, port: int, namespace: str, **_: Any) -> Iterator[int]:
        calls.append((service, str(port), namespace))
        yield int(client.config.base_url.rsplit(":", 1)[1])

    def secret_value(name: str, key: str, namespace: str, **_: Any) -> str:
        calls.append((name, namespace))
        return dict(zip(("ROOT_CLIENT_ID", "ROOT_CLIENT_SECRET"), ROOT, strict=True))[key]

    monkeypatch.setattr(k8s, "port_forward", port_forward)
    monkeypatch.setattr(k8s, "secret_value", secret_value)
    return calls


_TRINO = ("deployment/olf-system/trino-coordinator", "deployment/olf-system/trino-worker")


def _trino_restarts(cluster: Cluster) -> list[str]:
    """The Trino Deployments a run patched, which rolls them out."""
    return [argv[1] for argv in cluster.argv if argv[0] == "patch"]


def test_the_deploy_bootstraps_polaris_as_root_through_a_port_forward(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster, forwarded: list[tuple[str, ...]]
) -> None:
    fake, _ = polaris

    bootstrap_from_contract(_contracts(), environ=_ENVIRON)

    assert set(forwarded) == {("polaris", "8181", "olf-system"), ("polaris-bootstrap-credentials", "olf-system")}
    assert set(fake.catalogs) == {"lakehouse_dev", "lakehouse_prod"}
    assert "secret/olf-dev/polaris-dev-floe-creds" in cluster.load()


def test_trino_restarts_when_a_credential_it_reads_changes(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster, forwarded: list[tuple[str, ...]]
) -> None:
    for deployment in _TRINO:
        cluster.put(*deployment.split("/"), {})
    bootstrap_from_contract(_contracts(), environ=_ENVIRON)
    restarted = ["deployment/trino-coordinator", "deployment/trino-worker"]
    assert _trino_restarts(cluster) == restarted
    assert ["rollout", "status", "deployment/trino-worker", "-n", "olf-system", "--timeout=300s"] in cluster.argv
    cluster.argv.clear()
    # The window the pending marker exists for: Trino's credential is rotated on the next run.
    cluster.put("configmap", "olf-system", "polaris-prod-trino-creds-bootstrap-pending", {"principal": "trino-prod"})

    bootstrap_from_contract(_contracts(), environ=_ENVIRON)

    assert _trino_restarts(cluster) == restarted


def test_a_rerun_restarts_trino_when_the_run_that_rotated_its_credential_failed(
    polaris: tuple[FakePolaris, PolarisClient],
    cluster: Cluster,
    forwarded: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for deployment in _TRINO:
        cluster.put(*deployment.split("/"), {})
    bootstrap_from_contract(_contracts(), environ=_ENVIRON)
    cluster.put("configmap", "olf-system", "polaris-prod-trino-creds-bootstrap-pending", {"principal": "trino-prod"})
    # The run stores Trino's new credential, then fails before Trino restarts.
    with monkeypatch.context() as failing:
        failing.setattr(k8s, "annotate_pod_templates", Mock(side_effect=RuntimeError("API server gone")))
        with pytest.raises(RuntimeError):
            bootstrap_from_contract(_contracts(), environ=_ENVIRON)
    cluster.argv.clear()

    bootstrap_from_contract(_contracts(), environ=_ENVIRON)

    assert _trino_restarts(cluster) == ["deployment/trino-coordinator", "deployment/trino-worker"]


def test_trino_keeps_running_when_nothing_it_reads_changed(
    polaris: tuple[FakePolaris, PolarisClient], cluster: Cluster, forwarded: list[tuple[str, ...]]
) -> None:
    for deployment in _TRINO:
        cluster.put(*deployment.split("/"), {})
    bootstrap_from_contract(_contracts(), environ=_ENVIRON)
    cluster.argv.clear()

    bootstrap_from_contract(_contracts(), environ=_ENVIRON)
    # A removed-then-re-enabled DEV: its namespace's own credentials rotate, and Trino reads none of them.
    cluster.save({k: v for k, v in cluster.load().items() if not k.startswith("secret/olf-dev/")})
    bootstrap_from_contract(_contracts(), environ=_ENVIRON)

    assert "secret/olf-dev/polaris-dev-floe-creds" in cluster.load()
    assert _trino_restarts(cluster) == []


def test_nothing_is_reached_without_a_polaris_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "port_forward", Mock(side_effect=AssertionError("no Polaris to reach")))

    bootstrap_from_contract(_contracts(dev={"catalog": _GLUE}, prod={"catalog": _GLUE}), environ=_ENVIRON)
