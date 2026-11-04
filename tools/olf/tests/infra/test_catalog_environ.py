"""`olf infra catalog sync-namespaces` reads its settings from the contract environment it is handed.

Every variable lives only in the `ContractEnvironment` mapping: `os.environ` is scrubbed of them, so a read that
falls back to the ambient process environment sees defaults and the assertions below fail.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import boto3
import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.infra import auth, polaris
from olf.infra.commands import catalog
from olf.infra.deployment.contract_env import ContractEnvironment

REPO_ROOT = Path(__file__).resolve().parents[4]
runner = CliRunner()

_FAKE_KUBECTL = f"""#!{sys.executable}
import base64, json, os, socket, sys, time

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
    log.write(json.dumps({{"argv": args, "kubeconfig": os.environ.get("KUBECONFIG")}}) + "\\n")
if "port-forward" in args:
    port = int(args[args.index("port-forward") + 2].split(":")[0])
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen()
    time.sleep(60)
elif "secret" in args:
    secret = args[args.index("secret") + 1]
    key = args[-1].removeprefix("jsonpath={{.data.").removesuffix("}}")
    print(base64.b64encode(f"{{secret}}:{{key}}".encode()).decode(), end="")
"""


def _contract(tmp_path: Path, values: dict[str, str]) -> ContractEnvironment:
    return ContractEnvironment(
        environ={"OPENLAKEFORGE_REPO_ROOT": str(REPO_ROOT), **values},
        namespace=values.get("NAMESPACE", ""),
        kube_context=values.get("KUBE_CONTEXT", ""),
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
        if name.startswith(("OPENLAKEFORGE_", "OLF_", "AWS_")) or name in {"NAMESPACE", "KUBE_CONTEXT", "KUBECONFIG"}:
            monkeypatch.delenv(name)


def test_polaris_sync_forwards_and_authenticates_from_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(_FAKE_KUBECTL)
    kubectl.chmod(0o755)
    kubectl_log = tmp_path / "kubectl.log"
    # The executable search itself still reads the process PATH; everything else comes from the contract.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    contract = _contract(
        tmp_path,
        {
            "OLF_TOOLCHAIN_MODE": "host",
            "FAKE_KUBECTL_LOG": str(kubectl_log),
            "KUBE_CONTEXT": "kind-contract",
            "KUBECONFIG": "/contract/kubeconfig",
            "NAMESPACE": "stage-ns",
            "OPENLAKEFORGE_CATALOG_PROVIDER": "polaris",
            "OPENLAKEFORGE_CATALOG_REST_URI": "http://polaris.shared-ns:9191/api/catalog",
            "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_stage",
            "OPENLAKEFORGE_CATALOG_OAUTH_SCOPE": "PRINCIPAL_ROLE:deployer",
            "OPENLAKEFORGE_CATALOG_DEPLOYER_CREDENTIALS_SECRET_NAME": "deployer-secret",
            "OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_ID_KEY": "ID",
            "OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_SECRET_KEY": "SECRET",
            "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX": str(tmp_path / "pf"),
        },
    )
    _use_contract(monkeypatch, contract)
    clients: list[polaris.PolarisClient] = []
    monkeypatch.setattr(polaris.PolarisClient, "login", lambda self: None)
    monkeypatch.setattr(catalog, "reconcile_polaris", lambda client, **_kwargs: clients.append(client))

    result = runner.invoke(app, ["infra", "catalog", "sync-namespaces", "--dry-run"])

    assert result.exit_code == 0, result.output
    calls = [json.loads(line) for line in kubectl_log.read_text().splitlines()]
    forward = next(call["argv"] for call in calls if "port-forward" in call["argv"])
    assert forward[:3] == ["--context", "kind-contract", "port-forward"]
    assert forward[3] == "svc/polaris"
    assert forward[4].endswith(":9191")
    assert forward[5:] == ["-n", "shared-ns"]
    assert all(call["kubeconfig"] == "/contract/kubeconfig" for call in calls)
    assert (tmp_path / "pf-polaris-port-forward.log").exists()
    (client,) = clients
    assert client.config.catalog_name == "lakehouse_stage"
    assert client.config.oauth_scope == "PRINCIPAL_ROLE:deployer"
    assert client.config.client_id == "deployer-secret:ID"
    assert client.config.client_secret == "deployer-secret:SECRET"
    secret_reads = [call["argv"] for call in calls if "secret" in call["argv"]]
    assert all(argv[argv.index("-n") + 1] == "stage-ns" for argv in secret_reads)


def test_glue_sync_opens_its_session_and_catalog_from_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contract_env = {
        "OLF_HOME": str(tmp_path / "olf-home"),
        "OPENLAKEFORGE_CATALOG_PROVIDER": "aws-glue",
        "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_stage",
        "OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID": "123456789012",
        "OPENLAKEFORGE_CATALOG_GLUE_REGION": "eu-west-3",
    }
    auth.save_state("aws", {"source": "profile", "profile": "team-profile"}, contract_env)
    _use_contract(monkeypatch, _contract(tmp_path, contract_env))
    monkeypatch.setattr(auth.aws, "_aws_instance_profile_available", lambda: False)
    sessions: list[dict[str, object]] = []
    queried: list[dict[str, object]] = []
    glue_regions: list[object] = []

    class Paginator:
        def paginate(self, **kwargs: object) -> list[dict]:
            queried.append(kwargs)
            return [{"DatabaseList": []}]

    class GlueSdk:
        def get_paginator(self, name: str) -> Paginator:
            assert name == "get_databases"
            return Paginator()

    class Session:
        def client(self, service: str, **kwargs: object) -> GlueSdk:
            assert service == "glue"
            glue_regions.append(kwargs["region_name"])
            return GlueSdk()

    monkeypatch.setattr(boto3, "Session", lambda **kwargs: sessions.append(kwargs) or Session())

    result = runner.invoke(app, ["infra", "catalog", "sync-namespaces", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert sessions == [{"profile_name": "team-profile", "region_name": "eu-west-3"}]
    assert glue_regions == ["eu-west-3"]
    assert queried == [{"CatalogId": "123456789012"}]
    assert "lakehouse_stage_" in result.output  # the Glue database prefix derives from the contract's catalog name
