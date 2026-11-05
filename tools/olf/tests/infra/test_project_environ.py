"""The project/revision path reads its settings from the contract environment it is handed.

Every variable lives only in the `ContractEnvironment` mapping: `os.environ` is scrubbed of them, so a read that
falls back to the ambient process environment sees defaults (or nothing) and the assertions below fail.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest

from olf.infra import artifact_store, auth, s3
from olf.infra import contracts as contracts_module
from olf.infra.deployment import activation as activation_module
from olf.infra.deployment import contract_env, revision_build
from olf.infra.deployment.contract_env import ContractEnvironment

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
        environ=values,
        namespace=values.get("NAMESPACE", ""),
        kube_context=values.get("KUBE_CONTEXT", ""),
        kubeconfig_path=tmp_path / "kubeconfig",
        stage=None,
        unsets=(),
    )


@pytest.fixture(autouse=True)
def _scrub_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("OPENLAKEFORGE_", "OLF_", "AWS_")) or name in {"NAMESPACE", "KUBE_CONTEXT", "KUBECONFIG"}:
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


def _calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def _port_forward_contract(tmp_path: Path, log: Path) -> ContractEnvironment:
    return _contract(
        tmp_path,
        {
            "OLF_TOOLCHAIN_MODE": "host",
            "FAKE_KUBECTL_LOG": str(log),
            "KUBE_CONTEXT": "kind-contract",
            "KUBECONFIG": "/contract/kubeconfig",
            "NAMESPACE": "stage-ns",
            "OPENLAKEFORGE_SHARED_NAMESPACE": "shared-ns",
            "OPENLAKEFORGE_OPS_BUCKET_NAME": "ops-from-contract",
            "OPENLAKEFORGE_OPS_STORAGE_CREDENTIALS_SECRET_NAME": "ops-creds",
            "OPENLAKEFORGE_OPS_STORAGE_ACCESS_KEY_ID_KEY": "KEY_ID",
            "OPENLAKEFORGE_STORAGE_S3_SERVICE_NAME": "s3-svc",
            "OPENLAKEFORGE_STORAGE_S3_SERVICE_PORT": "9444",
            "OPENLAKEFORGE_STORAGE_REGION": "eu-west-3",
            "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX": str(tmp_path / "pf"),
        },
    )


class _S3Probe:
    """Stands in for the S3 SDK client the port-forward helper builds."""

    def __init__(self, heads: list[str], puts: list[dict[str, Any]]) -> None:
        self._heads = heads
        self._puts = puts

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803 - boto3's own keyword
        self._heads.append(Bucket)

    def put_object(self, **kwargs: Any) -> None:
        self._puts.append(kwargs)


def test_artifact_bucket_prefers_the_contracts_ops_bucket(tmp_path: Path) -> None:
    contract = _contract(
        tmp_path,
        {"OPENLAKEFORGE_OPS_BUCKET_NAME": "ops", "OPENLAKEFORGE_ARTIFACT_BUCKET_NAME": "legacy-artifacts"},
    )

    assert artifact_store.artifact_bucket(environ=contract) == "ops"


def test_artifact_bucket_ignores_the_ambient_environment_when_given_a_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_OPS_BUCKET_NAME", "ambient")

    with pytest.raises(artifact_store.ArtifactStoreError, match="no ops/artifact bucket"):
        artifact_store.artifact_bucket(environ=_contract(tmp_path, {}))


def test_artifact_bucket_still_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME", "legacy-artifacts")

    assert artifact_store.artifact_bucket() == "legacy-artifacts"


def test_direct_artifact_client_uses_the_contracts_aws_profile_and_region(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = {"OLF_HOME": str(tmp_path / "olf-home"), "OPENLAKEFORGE_STORAGE_REGION": "eu-west-1"}
    auth.save_state("aws", {"source": "profile", "profile": "team-profile"}, values)
    monkeypatch.setattr(auth.aws, "_aws_instance_profile_available", lambda: False)
    sessions: list[dict[str, object]] = []
    clients: list[tuple[str, dict[str, object]]] = []

    class Session:
        def client(self, service: str, **kwargs: object) -> object:
            clients.append((service, kwargs))
            return object()

    monkeypatch.setattr(boto3, "Session", lambda **kwargs: sessions.append(kwargs) or Session())

    with artifact_store.artifact_storage_client("direct", "ops", environ=_contract(tmp_path, values)):
        pass

    assert sessions == [{"profile_name": "team-profile", "region_name": "eu-west-1"}]
    assert [(service, kwargs["region_name"], kwargs["endpoint_url"]) for service, kwargs in clients] == [
        ("s3", "eu-west-1", None)
    ]


def test_direct_artifact_client_follows_the_contract_not_the_operators_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A contract that unsets `AWS_ENDPOINT_URL_S3` and sets `AWS_REGION` must win over the same names in the shell."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://operator-shell.invalid:9000")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    contract = {"OLF_HOME": str(tmp_path / "olf-home"), "AWS_REGION": "eu-west-1"}

    with artifact_store.artifact_storage_client("direct", "ops", environ=contract) as client:
        assert client.meta.region_name == "eu-west-1"
        assert "operator-shell" not in client.meta.endpoint_url

    contract["AWS_ENDPOINT_URL_S3"] = "http://contract-s3.invalid:8333"
    with artifact_store.artifact_storage_client("direct", "ops", environ=contract) as client:
        assert client.meta.endpoint_url == "http://contract-s3.invalid:8333"


def test_port_forward_artifact_client_resolves_the_store_from_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    contract = _port_forward_contract(tmp_path, kubectl_log)
    sdk: list[dict[str, Any]] = []
    heads: list[str] = []

    def client(service: str, **kwargs: Any) -> _S3Probe:
        assert service == "s3"
        sdk.append(kwargs)
        return _S3Probe(heads, [])

    monkeypatch.setattr(s3.boto3, "client", client)

    with artifact_store.artifact_storage_client("port-forward", "ops-from-contract", environ=contract):
        pass

    calls = _calls(kubectl_log)
    forward = next(call["argv"] for call in calls if "port-forward" in call["argv"])
    assert forward[:3] == ["--context", "kind-contract", "port-forward"]
    assert forward[3] == "svc/s3-svc"
    assert forward[4].endswith(":9444")
    assert forward[5:] == ["-n", "shared-ns"]
    secrets = [call["argv"] for call in calls if "secret" in call["argv"]]
    assert {argv[argv.index("secret") + 1] for argv in secrets} == {"ops-creds"}
    assert all(argv[argv.index("-n") + 1] == "shared-ns" for argv in secrets)
    assert all(call["kubeconfig"] == "/contract/kubeconfig" for call in calls)
    (kwargs,) = sdk
    assert kwargs["aws_access_key_id"] == "ops-creds:KEY_ID"
    assert kwargs["aws_secret_access_key"] == "ops-creds:AWS_SECRET_ACCESS_KEY"
    assert kwargs["region_name"] == "eu-west-3"
    assert heads == ["ops-from-contract"]
    assert (tmp_path / "pf-seaweedfs-port-forward.log").exists()


def test_provider_revision_store_publishes_to_the_contracts_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    contract = _port_forward_contract(tmp_path, kubectl_log)

    # Hand the contract over without writing `os.environ`, as the real one will.
    @contextmanager
    def applied(**_kwargs: object) -> Iterator[ContractEnvironment]:
        yield contract

    monkeypatch.setattr(contract_env, "applied_contract_environment", applied)
    puts: list[dict[str, Any]] = []
    monkeypatch.setattr(s3.boto3, "client", lambda _service, **_kwargs: _S3Probe([], puts))
    provider: Any = SimpleNamespace(env={}, backend=None)
    context: Any = SimpleNamespace(
        provider=SimpleNamespace(value="local"),
        namespace="stage-ns",
        kube_context="kind-contract",
        paths=SimpleNamespace(
            platform_terraform_dir=tmp_path,
            kubeconfig_path=tmp_path / "kubeconfig",
            port_forward_log_prefix=tmp_path / "pf",
        ),
        topology=None,
        stage="dev",
    )

    with revision_build.provider_revision_store(provider, context, tmp_path) as store:
        store.write("revisions/r/MANIFEST.json", b"{}", content_type="application/json")

    assert [put["Bucket"] for put in puts] == ["ops-from-contract"]
    assert puts[0]["Key"] == "revisions/r/MANIFEST.json"


def test_activation_webserver_forward_runs_kubectl_under_the_contract_environment(
    tmp_path: Path, kubectl_log: Path
) -> None:
    contract = _port_forward_contract(tmp_path, kubectl_log)

    with activation_module._stage_webserver(
        "stage-ns", kube_context="kind-contract", log_prefix=str(tmp_path / "pf"), environ=contract
    ) as client:
        assert client is not None

    (call,) = _calls(kubectl_log)
    assert call["argv"][:3] == ["--context", "kind-contract", "port-forward"]
    assert call["argv"][5:] == ["-n", "stage-ns"]
    assert call["kubeconfig"] == "/contract/kubeconfig"


def test_the_inner_contract_sees_the_defaults_the_outer_contract_supplied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`provider_revision_store` wraps activation: the outer block's environment sits under the inner one.

    A value the contract only defaults (the OpenMetadata catalog database) keeps the outer stage's value, because the
    inner block derives its base from the process environment the outer one wrote."""
    stage_catalogs = {"dev": "lakehouse_dev", "prod": "lakehouse_prod"}

    def build_contract_env(base: Any, _contracts: Any, *, stage: str, **_kwargs: Any) -> tuple[dict[str, str], list]:
        return {"OPENMETADATA_CATALOG_DATABASE": base.get("OPENMETADATA_CATALOG_DATABASE") or stage_catalogs[stage]}, []

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda *_a, **_k: None)
    monkeypatch.setattr(contracts_module, "build_contract_env", build_contract_env)
    kwargs: dict[str, Any] = {
        "contract_terraform_dir": tmp_path,
        "repo_root": tmp_path,
        "namespace": "olf",
        "kube_context": "kind",
        "kubeconfig_path": tmp_path / "kubeconfig",
        "port_forward_log_prefix": tmp_path / "pf",
    }

    with contract_env.applied_contract_environment(**kwargs, stage="dev") as outer:
        assert outer["OPENMETADATA_CATALOG_DATABASE"] == "lakehouse_dev"
        with contract_env.applied_contract_environment(**kwargs, stage="prod") as inner:
            assert inner["OPENMETADATA_CATALOG_DATABASE"] == "lakehouse_dev"
            assert os.environ["OPENMETADATA_CATALOG_DATABASE"] == "lakehouse_dev"
        assert outer["OPENMETADATA_CATALOG_DATABASE"] == "lakehouse_dev"

    assert "OPENMETADATA_CATALOG_DATABASE" not in os.environ
