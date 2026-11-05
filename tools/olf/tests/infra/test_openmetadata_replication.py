"""The platform phase copies the ingestion-bot Secret into governed stage namespaces (#562)."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from conftest import CONFORMANCE_PROFILE

from olf.common.deployment.errors import CommandExecutionError
from olf.common.profile import StageName
from olf.common.provider_contracts import ProviderContracts, parse_provider_contracts
from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import CommandResult
from olf.infra import k8s
from olf.infra.commands._shared import deployment_context_for_profile
from olf.infra.commands.openmetadata import bootstrap_from_contract
from olf.infra.openmetadata._replication import replicate_ingestion_bot, replication_namespaces

_CONTRACT = json.loads((Path(__file__).parents[1] / "fixtures" / "conformance-provider-contracts.json").read_text())
_SECRET = "openmetadata-ingestion-bot"
_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJpbmdlc3Rpb24tYm90In0.c2lnbmF0dXJl"
_ENVIRON = {"KUBE_CONTEXT": "kind-test"}


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _secret(namespace: str, data: dict[str, str], **metadata: Any) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": _SECRET, "namespace": namespace, **metadata},
        "type": "Opaque",
        "data": {key: _b64(value) for key, value in data.items()},
    }


class _Cluster:
    """Secrets by (namespace, name), served to the kubectl argv olf runs; records every argv."""

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], dict[str, Any]] = {}
        self.argv: list[list[str]] = []

    def writes(self) -> list[list[str]]:
        return [argv for argv in self.argv if argv[0] in {"create", "delete", "apply"}]

    def run(self, command: Any, *, check: bool = True, input_text: str | None = None, **_: Any) -> CommandResult:
        argv = [str(part) for part in command][3:]  # drop kubectl --context kind-test
        self.argv.append(argv)
        stdout, returncode = "", 0
        if argv[:2] == ["get", "secret"]:
            found = self.secrets.get((argv[4], argv[2]))
            stdout = json.dumps(found) if found else ""
        elif argv[:2] == ["delete", "secret"]:
            self.secrets.pop((argv[4], argv[2]), None)
        elif argv[0] in {"create", "apply"}:
            manifest = json.loads(input_text or "{}")
            key = (manifest["metadata"]["namespace"], manifest["metadata"]["name"])
            data = {name: _b64(value) for name, value in manifest.pop("stringData").items()}
            if argv[0] == "create":
                returncode = 1 if key in self.secrets else 0
                self.secrets.setdefault(key, {**manifest, "data": data})
            else:  # a server-side apply keeps the keys another field manager wrote
                existing = self.secrets.get(key, {}).get("data", {})
                self.secrets[key] = {**manifest, "data": {**existing, **data}}
        else:
            raise AssertionError(f"unexpected kubectl call: {argv}")
        if check and returncode:
            raise CommandExecutionError(command, returncode, stderr="AlreadyExists")
        return CommandResult(tuple(argv), returncode, stdout, "", 0.0)


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Cluster:
    cluster = _Cluster()
    resolver = Mock()
    resolver.resolve.return_value = tmp_path / "kubectl"
    monkeypatch.setattr(k8s, "ProcessRunner", lambda: cluster)
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda *_a: "kubectl")
    monkeypatch.setattr(k8s.Toolkit, "default", lambda **_k: SimpleNamespace(kubectl=Kubectl(cluster, resolver)))
    cluster.secrets[("olf-system", _SECRET)] = _secret("olf-system", {"OPENMETADATA_INGESTION_BOT_JWT": _JWT})
    return cluster


def _contracts(**governance: bool) -> ProviderContracts:
    topology = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value).topology
    contracts = parse_provider_contracts(_CONTRACT, topology)
    stages = {
        name: stage if governance.get(name.value, True) else replace(stage, governance=None)
        for name, stage in contracts.stages.items()
    }
    return replace(contracts, stages=stages)


def _replicate(contracts: ProviderContracts, shared: str = "olf-system") -> None:
    governed, revoked = replication_namespaces(contracts)
    replicate_ingestion_bot(shared, governed, revoked, environ=_ENVIRON)


def test_each_governed_stage_gets_an_exact_copy_and_the_others_lose_theirs(cluster: _Cluster) -> None:
    # An earlier copy with a key the source no longer has, and one left from when PROD was governed.
    cluster.secrets[("olf-dev", _SECRET)] = _secret("olf-dev", {"OPENMETADATA_INGESTION_BOT_JWT": "old", "stale": "x"})
    cluster.secrets[("olf-prod", _SECRET)] = _secret("olf-prod", {"OPENMETADATA_INGESTION_BOT_JWT": _JWT})

    _replicate(_contracts(prod=False))

    # What `kubectl create secret generic --from-literal` makes: the source's keys, no labels or annotations.
    assert cluster.secrets[("olf-dev", _SECRET)] == _secret("olf-dev", {"OPENMETADATA_INGESTION_BOT_JWT": _JWT})
    assert ("olf-prod", _SECRET) not in cluster.secrets
    assert ("olf-system", _SECRET) in cluster.secrets


def test_every_governed_stage_is_a_target(cluster: _Cluster) -> None:
    _replicate(_contracts())

    assert {namespace for namespace, _ in cluster.secrets} == {"olf-system", "olf-dev", "olf-prod"}


def test_a_rerun_changes_nothing(cluster: _Cluster) -> None:
    _replicate(_contracts(prod=False))
    copied = dict(cluster.secrets)
    cluster.argv.clear()

    _replicate(_contracts(prod=False))

    assert cluster.secrets == copied
    assert cluster.writes() == []


def test_the_token_is_never_in_argv_or_output(cluster: _Cluster, capsys: pytest.CaptureFixture[str]) -> None:
    cluster.secrets[("olf-prod", _SECRET)] = _secret("olf-prod", {"OPENMETADATA_INGESTION_BOT_JWT": _JWT})

    _replicate(_contracts(prod=False))

    output = capsys.readouterr()
    assert "Replicated Secret 'openmetadata-ingestion-bot' into 'olf-dev'." in output.out
    assert "Revoked Secret 'openmetadata-ingestion-bot' from 'olf-prod'." in output.out
    for text in (output.out + output.err, json.dumps(cluster.argv)):
        assert _JWT not in text and _b64(_JWT) not in text


def test_a_stage_in_the_shared_namespace_neither_copies_nor_revokes_the_source(cluster: _Cluster) -> None:
    # The single-namespace cloud roots run every stage beside OpenMetadata.
    replicate_ingestion_bot("olf-system", ["olf-system"], ["olf-system"], environ=_ENVIRON)

    assert cluster.secrets[("olf-system", _SECRET)] == _secret("olf-system", {"OPENMETADATA_INGESTION_BOT_JWT": _JWT})
    assert cluster.writes() == []


def test_a_missing_source_fails(cluster: _Cluster) -> None:
    cluster.secrets.clear()

    with pytest.raises(k8s.KubectlError, match="'openmetadata-ingestion-bot' is missing in 'olf-system'"):
        _replicate(_contracts())


def test_nothing_is_copied_or_revoked_when_no_stage_is_governed(cluster: _Cluster) -> None:
    cluster.secrets[("olf-prod", _SECRET)] = _secret("olf-prod", {"OPENMETADATA_INGESTION_BOT_JWT": _JWT})

    bootstrap_from_contract(_contracts(dev=False, prod=False), environ=_ENVIRON)

    assert cluster.argv == []
