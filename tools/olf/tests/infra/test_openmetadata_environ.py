"""The OpenMetadata commands read their settings from the contract environment they are handed.

Every variable lives only in the `ContractEnvironment` mapping: `os.environ` is scrubbed of them, so a read that
falls back to the ambient process environment sees defaults (or no cluster at all) and the assertions below fail.
Only the network edges are stubbed: the port-forward, the OpenMetadata deployer and SSO (still ambient, a later PR).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FakeOpenMetadata
from typer.testing import CliRunner

from olf.cli import app
from olf.infra import access
from olf.infra import openmetadata as om
from olf.infra.deployment import artifact_steps
from olf.infra.deployment.contract_env import ContractEnvironment
from olf.infra.openmetadata import _bootstrap, _role_sync, _sso, _stale_services

REPO_ROOT = Path(__file__).resolve().parents[4]
runner = CliRunner()

_FAKE_KUBECTL = f"""#!{sys.executable}
import base64, json, os, sys

argv = sys.argv[1:]
record = {{"argv": argv, "kubeconfig": os.environ.get("KUBECONFIG")}}
if "-f" in argv:
    with open(argv[argv.index("-f") + 1]) as handle:
        record["manifest"] = json.load(handle)
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
if "secret" in argv:
    path = next(arg for arg in argv if arg.startswith("jsonpath="))
    key = path.removeprefix("jsonpath={{.data.").removesuffix("}}")
    value = json.loads(os.environ["FAKE_KUBECTL_SECRETS"]).get(key, "")
    sys.stdout.write(base64.b64encode(value.encode()).decode())
elif any(arg.startswith("jsonpath={{.status.conditions") for arg in argv):
    sys.stdout.write("Complete")
"""


def _contract(tmp_path: Path, values: dict[str, str]) -> ContractEnvironment:
    return ContractEnvironment(
        environ=values,
        namespace=values["OPENLAKEFORGE_KUBE_NAMESPACE"],
        kube_context=values["KUBE_CONTEXT"],
        kubeconfig_path=tmp_path / "kubeconfig",
        stage=None,
        unsets=(),
    )


@pytest.fixture(autouse=True)
def _scrub_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("OPENLAKEFORGE_", "OLF_", "OPENMETADATA_")) or name in {
            "NAMESPACE",
            "KUBE_CONTEXT",
            "KUBECONFIG",
        }:
            monkeypatch.delenv(name)


@pytest.fixture
def kubectl_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(_FAKE_KUBECTL)
    kubectl.chmod(0o755)
    # The executable search itself still reads the process PATH; everything else comes from the contract.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "kubectl.log"


def _calls(kubectl_log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in kubectl_log.read_text().splitlines()]


def _om_contract(tmp_path: Path, kubectl_log: Path, project: Path, **extra: str) -> ContractEnvironment:
    return _contract(
        tmp_path,
        {
            "OLF_TOOLCHAIN_MODE": "host",
            "FAKE_KUBECTL_LOG": str(kubectl_log),
            "FAKE_KUBECTL_SECRETS": json.dumps(
                {
                    "OPENMETADATA_AUTOMATION_JWT": FakeOpenMetadata.token,
                    "OPENMETADATA_INGESTION_BOT_JWT": "ingestion-jwt",
                    "admin-password": "stored-admin",
                }
            ),
            "KUBE_CONTEXT": "kind-contract",
            "KUBECONFIG": "/contract/kubeconfig",
            "OPENLAKEFORGE_REPO_ROOT": str(REPO_ROOT),
            "OPENLAKEFORGE_PROJECT_ROOT": str(project),
            "OPENLAKEFORGE_GOVERNANCE_ENABLED": "true",
            "OPENLAKEFORGE_ANALYTICS_ENABLED": "false",
            "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
            "OPENLAKEFORGE_KUBE_NAMESPACE": "stage-ns",
            "OPENLAKEFORGE_SHARED_NAMESPACE": "shared-ns",
            "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX": str(tmp_path / "pf"),
            "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_contract",
            "OPENLAKEFORGE_CATALOG_PROVIDER": "polaris-contract",
            "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": "{}",
            "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
            "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.contract",
            "OPENLAKEFORGE_QUERY_TRINO_PORT": "9090",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME": "ingestion-bot-contract",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY": "JWT_CONTRACT",
            "OPENMETADATA_SERVICE": "om-contract",
            "OPENMETADATA_SERVICE_PORT": "9191",
            "OPENMETADATA_ADMIN_EMAIL": "contract-admin@example.test",
            "OPENMETADATA_ADMIN_PASSWORD": "contract-password",
            **extra,
        },
    )


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch, fake_openmetadata: FakeOpenMetadata) -> list[dict[str, object]]:
    """Stand in for the port-forward (the network edge): serve the fake OpenMetadata, record where it was asked for."""
    requests: list[dict[str, object]] = []

    @contextlib.contextmanager
    def service_route(ref: str, **kwargs: object) -> Iterator[access.Route]:
        requests.append({"ref": ref, **kwargs})
        yield access.Route(fake_openmetadata.url)

    monkeypatch.setattr(access, "service_route", service_route)
    return requests


def _use_contract(monkeypatch: pytest.MonkeyPatch, contract: ContractEnvironment) -> None:
    """Hand `contract` to the command without writing `os.environ`, as `provider_contract_environment` will."""

    @contextmanager
    def provider_contract_environment(**_kwargs: str) -> Iterator[ContractEnvironment]:
        yield contract

    monkeypatch.setattr("olf.infra.commands.runtime.provider_contract_environment", provider_contract_environment)


def _assert_forward_targets_the_contract(forwarded: list[dict[str, object]], tmp_path: Path) -> None:
    (request,) = forwarded
    assert request["ref"] == "shared/governance_service"
    assert request["service"] == "om-contract"
    assert request["remote_port"] == 9191
    assert request["namespace"] == "shared-ns"
    assert request["log_path"] == f"{tmp_path / 'pf'}-openmetadata-port-forward.log"


def test_list_domains_authenticates_with_the_token_kubectl_reads_for_the_contract_cluster(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kubectl_log: Path,
    forwarded: list[dict[str, object]],
    fake_openmetadata: FakeOpenMetadata,
) -> None:
    fake_openmetadata.domains = [{"name": "sales", "displayName": "Sales"}]
    _use_contract(monkeypatch, _om_contract(tmp_path, kubectl_log, tmp_path))

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains", "--json"])

    # The fake answers 401 unless the bearer is the Secret value kubectl returned for the contract's cluster.
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"name": "sales", "displayName": "Sales"}]
    (secret_read,) = _calls(kubectl_log)
    assert secret_read["argv"][:2] == ["--context", "kind-contract"]
    assert secret_read["argv"][2:6] == ["get", "secret", "openmetadata-automation-bot", "-n"]
    assert secret_read["argv"][6] == "shared-ns"
    assert secret_read["kubeconfig"] == "/contract/kubeconfig"
    _assert_forward_targets_the_contract(forwarded, tmp_path)


def test_list_domains_honours_a_governance_layer_disabled_in_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    contract = _om_contract(tmp_path, kubectl_log, tmp_path, OPENLAKEFORGE_GOVERNANCE_ENABLED="false")
    _use_contract(monkeypatch, contract)

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains"])

    assert result.exit_code != 0
    assert "governance layer is disabled" in result.output


def test_deploy_flow_seeds_the_catalog_and_crawls_trino_for_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kubectl_log: Path,
    forwarded: list[dict[str, object]],
    fake_openmetadata: FakeOpenMetadata,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    contract = _om_contract(
        tmp_path,
        kubectl_log,
        project,
        OPENMETADATA_METADATA_ROOT=str(tmp_path / "metadata"),
        OPENMETADATA_ADMIN_PASSWORD="",
    )
    deployed: list[om.OpenMetadataDeployer] = []

    class Deployer:
        def __init__(self, cfg: om.OpenMetadataConfig, client: om.OpenMetadataClient) -> None:
            self.cfg, self.client = cfg, client

        def deploy(self) -> None:
            deployed.append(self)  # type: ignore[arg-type]  # test double standing in for the seeding network edge

    # SSO still reads the ambient environment (PR 7); it is a no-op here, as for a profile without identity.
    monkeypatch.setattr(_sso, "apply_sso", lambda *_a, **_k: None)
    monkeypatch.setattr(om, "OpenMetadataDeployer", Deployer)

    artifact_steps.deploy_optional_layer_artifacts(contract, allow_missing_assets=True)

    (seeded,) = deployed
    assert seeded.cfg.base_url == fake_openmetadata.url
    assert seeded.client.token == FakeOpenMetadata.token
    assert seeded.cfg.catalog_service == "polaris-contract"
    assert seeded.cfg.catalog_database == "lakehouse_contract"
    assert seeded.cfg.metadata_root == tmp_path / "metadata"
    assert seeded.cfg.allow_missing_assets is True
    _assert_forward_targets_the_contract(forwarded, tmp_path)

    calls = _calls(kubectl_log)
    assert all(call["argv"][:2] == ["--context", "kind-contract"] for call in calls)
    assert all(call["kubeconfig"] == "/contract/kubeconfig" for call in calls)
    rollout = next(call for call in calls if "rollout" in call["argv"])
    assert rollout["argv"][2:] == ["rollout", "status", "deployment/om-contract", "-n", "shared-ns", "--timeout=300s"]
    manifests = {call["manifest"]["kind"]: call["manifest"] for call in calls if "manifest" in call}
    assert set(manifests) == {"CronJob", "Job"}
    assert manifests["CronJob"]["metadata"]["name"].endswith("lakehouse-contract")
    pod = manifests["Job"]["spec"]["template"]["spec"]["containers"][0]
    workflow = yaml.safe_load(next(item["value"] for item in pod["env"] if item["name"] == "OM_WORKFLOW"))
    assert workflow["source"]["serviceName"] == "polaris-contract"
    assert workflow["source"]["serviceConnection"]["config"]["hostPort"] == "trino.contract:9090"
    assert workflow["workflowConfig"]["openMetadataServerConfig"]["hostPort"] == "http://om-contract.shared-ns:9191/api"
    token_ref = next(item for item in pod["env"] if item["name"] == "OM_BOT_JWT")["valueFrom"]["secretKeyRef"]
    assert token_ref == {"name": "ingestion-bot-contract", "key": "JWT_CONTRACT"}


def test_role_sync_reads_the_deployment_profile_from_the_contract_project_root(
    tmp_path: Path, kubectl_log: Path
) -> None:
    contract = _om_contract(tmp_path, kubectl_log, tmp_path / "empty-project")

    # The ambient project root defaults to this checkout, which does have a Deployment Profile.
    assert "no Deployment Profile" in _role_sync.apply_role_sync("shared-ns", om_url="http://om", environ=contract)


def test_bootstrap_logs_in_with_the_contract_admin_email_and_the_password_kubectl_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    contract = _om_contract(tmp_path, kubectl_log, tmp_path)
    _use_contract(monkeypatch, contract)
    logins: list[tuple[str, str, str]] = []

    class Client:
        base_url = "http://om.test"
        token = "session"

        def request(self, method: str, path: str, **_kwargs: object) -> dict[str, str]:
            assert (method, path) == ("GET", "/api/v1/users/name/ingestion-bot")
            return {"id": "bot-id"}

    def admin_client(base_url: str, email: str, password: str, stored_token: str | None) -> Client:
        logins.append((email, password, stored_token or ""))
        return Client()

    monkeypatch.setattr(_bootstrap, "wait_healthy", lambda _base_url: None)
    monkeypatch.setattr(_bootstrap, "admin_client", admin_client)
    monkeypatch.setattr(_bootstrap, "_token_works", lambda *_a, **_k: True)  # the network edge
    monkeypatch.setattr(_stale_services, "remove_legacy_services", lambda _client: None)

    result = runner.invoke(app, ["infra", "openmetadata", "bootstrap", "--om-url", "http://om.test"])

    assert result.exit_code == 0, result.output
    assert logins == [("contract-admin@example.test", "stored-admin", FakeOpenMetadata.token)]
    calls = _calls(kubectl_log)
    assert calls and all(call["argv"][:2] == ["--context", "kind-contract"] for call in calls)
    assert all(call["argv"][call["argv"].index("-n") + 1] == "shared-ns" for call in calls)
