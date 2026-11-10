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
from olf.infra.openmetadata._replication import replicate_ingestion_bot, replication_namespaces, revoke_trino_crawl

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
        # The Trino crawl's workloads, by (namespace, kind); olf deletes them by label, never by name.
        self.crawls: set[tuple[str, str]] = set()
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
        elif argv[0] == "delete" and argv[1] in {"cronjob", "job"}:
            assert argv[2:4] == ["-l", "openlakeforge.io/job=openmetadata-trino-ingestion"]
            self.crawls.discard((argv[5], argv[1]))
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


_OM_CREDS = "polaris-om-creds"


def _with_crawl(cluster: _Cluster, namespace: str) -> None:
    """What `olf infra openmetadata deploy-metadata` leaves beside a governed stage's coordinator."""
    cluster.secrets[(namespace, _OM_CREDS)] = {
        **_secret(namespace, {"POLARIS_OM_CLIENT_ID": "openmetadata", "POLARIS_OM_CLIENT_SECRET": "s" * 32}),
        "metadata": {"name": _OM_CREDS, "namespace": namespace},
    }
    cluster.crawls |= {(namespace, "cronjob"), (namespace, "job")}


def test_a_stage_that_drops_governance_loses_the_crawl_and_its_polaris_credential(cluster: _Cluster) -> None:
    for namespace in ("olf-system", "olf-dev", "olf-prod"):
        _with_crawl(cluster, namespace)
    _, revoked = replication_namespaces(_contracts(prod=False))

    revoke_trino_crawl("olf-system", revoked, environ=_ENVIRON)

    assert ("olf-prod", _OM_CREDS) not in cluster.secrets
    assert {namespace for namespace, _ in cluster.crawls} == {"olf-system", "olf-dev"}
    assert ("olf-dev", _OM_CREDS) in cluster.secrets and ("olf-system", _OM_CREDS) in cluster.secrets


def test_the_shared_namespace_keeps_the_crawl_credential_the_governed_stages_copy(cluster: _Cluster) -> None:
    # The single-namespace cloud roots run every stage, and every crawl, beside Polaris.
    _with_crawl(cluster, "olf-system")

    revoke_trino_crawl("olf-system", ["olf-system"], environ=_ENVIRON)

    assert ("olf-system", _OM_CREDS) in cluster.secrets
    assert cluster.writes() == []


def test_with_no_governed_stage_the_crawl_is_revoked_and_nothing_else_changes(cluster: _Cluster) -> None:
    # The `openmetadata` principal outlives the last governed stage, so its copy must go even though
    # there is no OpenMetadata left to bootstrap; the ingestion-bot copy names a service that is gone.
    cluster.secrets[("olf-prod", _SECRET)] = _secret("olf-prod", {"OPENMETADATA_INGESTION_BOT_JWT": _JWT})
    _with_crawl(cluster, "olf-prod")

    bootstrap_from_contract(_contracts(dev=False, prod=False), environ=_ENVIRON)

    assert ("olf-prod", _OM_CREDS) not in cluster.secrets
    assert ("olf-prod", "cronjob") not in cluster.crawls
    assert ("olf-prod", _SECRET) in cluster.secrets and ("olf-system", _SECRET) in cluster.secrets
