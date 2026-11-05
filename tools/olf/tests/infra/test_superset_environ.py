"""The Superset report commands read their settings from the contract environment they are handed.

Every variable lives only in the `ContractEnvironment` mapping: `os.environ` is scrubbed of them, so a read that
falls back to the ambient process environment sees defaults and the assertions below fail.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml
from _report_bundle_support import DASHBOARD_UUID, REPORT_DIR, write_report_bundle
from conftest import FakeSuperset
from typer.testing import CliRunner

from olf.cli import app
from olf.infra import access
from olf.infra.deployment import artifact_steps
from olf.infra.deployment.contract_env import ContractEnvironment

REPO_ROOT = Path(__file__).resolve().parents[4]
runner = CliRunner()

_FAKE_KUBECTL = f"""#!{sys.executable}
import json, os, sys

with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "kubeconfig": os.environ.get("KUBECONFIG")}}) + "\\n")
"""


def _seed_project(root: Path) -> Path:
    """A minimal valid project with a draft report bundle and no declared dashboards."""
    source_dir = root / "lakehouse_code" / "bronze" / "crm"
    source_dir.mkdir(parents=True)
    (source_dir / "source.yaml").write_text(
        "apiVersion: openlakeforge.io/v1alpha3\nkind: Source\nname: crm\ndisplayName: CRM\n"
        "description: CRM source.\nstatus: planned\nresources:\n  - name: orders\n"
    )
    (root / "lakehouse_code" / "governance.yml").write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: Governance\n"
        "domains:\n  - name: sales\n    displayName: Sales\n    description: Sales domain.\n    status: planned\n"
        "products:\n  - id: orders\n    domain: sales\n    displayName: Orders\n    description: Orders.\n"
        "    status: planned\n"
    )
    return write_report_bundle(root).parent.parent.parent.parent


def _contract(tmp_path: Path, values: dict[str, str]) -> ContractEnvironment:
    return ContractEnvironment(
        environ=values,
        namespace=values["OPENLAKEFORGE_KUBE_NAMESPACE"],
        kube_context=values["KUBE_CONTEXT"],
        kubeconfig_path=tmp_path / "kubeconfig",
        stage=None,
        unsets=(),
    )


def _use_contract(monkeypatch: pytest.MonkeyPatch, contract: ContractEnvironment) -> None:
    """Hand `contract` to the command without writing `os.environ`, as `provider_contract_environment` will."""

    @contextmanager
    def provider_contract_environment(**_kwargs: str) -> Iterator[ContractEnvironment]:
        yield contract

    monkeypatch.setattr("olf.infra.commands.runtime.provider_contract_environment", provider_contract_environment)


@pytest.fixture(autouse=True)
def _scrub_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("OPENLAKEFORGE_", "OLF_", "SUPERSET_")) or name in {
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


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch, fake_superset: FakeSuperset) -> list[dict[str, object]]:
    """Stand in for the port-forward (the network edge): serve the fake Superset and record where it was asked for."""
    requests: list[dict[str, object]] = []

    @contextlib.contextmanager
    def service_route(ref: str, **kwargs: object) -> Iterator[access.Route]:
        requests.append({"ref": ref, **kwargs})
        yield access.Route(fake_superset.url)

    monkeypatch.setattr(access, "service_route", service_route)
    return requests


def _report_contract(tmp_path: Path, kubectl_log: Path, project: Path, **extra: str) -> ContractEnvironment:
    return _contract(
        tmp_path,
        {
            "OLF_TOOLCHAIN_MODE": "host",
            "FAKE_KUBECTL_LOG": str(kubectl_log),
            "KUBE_CONTEXT": "kind-contract",
            "KUBECONFIG": "/contract/kubeconfig",
            "OPENLAKEFORGE_REPO_ROOT": str(REPO_ROOT),
            "OPENLAKEFORGE_PROJECT_ROOT": str(project),
            "OPENLAKEFORGE_ANALYTICS_ENABLED": "true",
            "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
            "OPENLAKEFORGE_KUBE_NAMESPACE": "stage-ns",
            "OPENLAKEFORGE_SHARED_NAMESPACE": "shared-ns",
            "OPENLAKEFORGE_DBT_TRINO_USER": "olf-contract",
            "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.contract",
            "OPENLAKEFORGE_QUERY_TRINO_PORT": "9090",
            "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_contract",
            "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX": str(tmp_path / "pf"),
            "SUPERSET_ADMIN_USERNAME": "admin",
            "SUPERSET_ADMIN_PASSWORD": "secret",
            "SUPERSET_REPORT_WORK_DIR": str(tmp_path / "work"),
            **extra,
        },
    )


def _assert_cluster_calls_use_the_contract(kubectl_log: Path) -> None:
    calls = [json.loads(line) for line in kubectl_log.read_text().splitlines()]
    (rollout,) = [call for call in calls if "rollout" in call["argv"]]
    assert rollout["argv"][:2] == ["--context", "kind-contract"]
    assert rollout["argv"][2:] == ["rollout", "status", "deployment/superset", "-n", "stage-ns", "--timeout=300s"]
    assert rollout["kubeconfig"] == "/contract/kubeconfig"


def _assert_forward_targets_the_contract(forwarded: list[dict[str, object]], tmp_path: Path) -> None:
    (request,) = forwarded
    assert request["ref"] == "stage/dev/reporting"
    assert request["namespace"] == "stage-ns"
    assert request["shared_namespace"] == "shared-ns"
    assert request["log_path"] == f"{tmp_path / 'pf'}-superset-port-forward.log"


def _import_command(contract: ContractEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_contract(monkeypatch, contract)
    result = runner.invoke(app, ["infra", "report", "import", "--stage", "dev"])
    assert result.exit_code == 0, result.output


def _import_through_the_deploy_flow(contract: ContractEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch  # the deploy flow hands its contract environment straight to the layer deploy
    artifact_steps.deploy_optional_layer_artifacts({**contract, "OPENLAKEFORGE_GOVERNANCE_ENABLED": "false"})


@pytest.mark.parametrize("entry", [_import_command, _import_through_the_deploy_flow], ids=["command", "deploy-flow"])
def test_report_import_targets_the_stage_and_credentials_of_the_contract_environment(
    entry: Callable[[ContractEnvironment, pytest.MonkeyPatch], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kubectl_log: Path,
    forwarded: list[dict[str, object]],
    fake_superset: FakeSuperset,
) -> None:
    contract = _report_contract(tmp_path, kubectl_log, REPO_ROOT)
    schemas = ("customer_health_gold", "inventory_reliability_gold", "order_revenue_gold")
    for permission_id, schema in enumerate(schemas, start=1):
        fake_superset.permissions[permission_id] = ("schema_access", f"[OpenLakeForge Trino].[{schema}]")

    entry(contract, monkeypatch)

    assert fake_superset.imported
    for imported in fake_superset.imported:
        with ZipFile(io.BytesIO(imported)) as bundle:
            databases = [yaml.safe_load(bundle.read(name)) for name in bundle.namelist() if "/databases/" in name]
        assert {database["sqlalchemy_uri"] for database in databases} == {
            "trino://olf-contract@trino.contract:9090/lakehouse_contract"
        }
    assert (tmp_path / "work").is_dir()  # the import's scratch directory came from SUPERSET_REPORT_WORK_DIR
    _assert_cluster_calls_use_the_contract(kubectl_log)
    _assert_forward_targets_the_contract(forwarded, tmp_path)


def test_report_export_reads_bundle_dashboard_and_credentials_from_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kubectl_log: Path,
    forwarded: list[dict[str, object]],
    fake_superset: FakeSuperset,
) -> None:
    project = tmp_path / "project"
    _seed_project(project)
    target = project / REPORT_DIR
    export = tmp_path / "export.zip"
    with ZipFile(export, "w") as bundle:
        bundle.writestr("exported/metadata.yaml", "version: 1.0.0\ntype: Dashboard\n")
        bundle.writestr("exported/dashboards/Renamed_9.yaml", f"dashboard_title: Renamed\nuuid: {DASHBOARD_UUID}\n")
    fake_superset.dashboards["contract-dashboard"] = 9
    fake_superset.exports[9] = export.read_bytes()
    contract = _report_contract(
        tmp_path,
        kubectl_log,
        project,
        SUPERSET_REPORT_SOURCE_DIR=REPORT_DIR,
        SUPERSET_DASHBOARD="contract-dashboard",
        SUPERSET_REPORT_EXPORT_BUNDLE_NAME="contract-bundle.zip",
    )
    _use_contract(monkeypatch, contract)

    result = runner.invoke(app, ["infra", "report", "export", "--stage", "dev"])

    assert result.exit_code == 0, result.output
    assert [path.name for path in (target / "dashboards").iterdir()] == ["Renamed_9.yaml"]
    assert (tmp_path / "work" / "contract-bundle.zip").is_file()
    _assert_cluster_calls_use_the_contract(kubectl_log)
    _assert_forward_targets_the_contract(forwarded, tmp_path)


def test_export_refuses_the_retired_dashboard_title_variable_set_in_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    project = tmp_path / "project"
    _seed_project(project)
    contract = _report_contract(
        tmp_path, kubectl_log, project, SUPERSET_REPORT_SOURCE_DIR=REPORT_DIR, SUPERSET_DASHBOARD_TITLE="Orders"
    )
    _use_contract(monkeypatch, contract)

    result = runner.invoke(app, ["infra", "report", "export", "--stage", "dev"])

    assert result.exit_code != 0
    assert "SUPERSET_DASHBOARD_TITLE is no longer read" in result.output
