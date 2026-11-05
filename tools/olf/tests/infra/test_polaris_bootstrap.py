"""olf's Polaris bootstrap: stage catalogs and principal credentials (#562)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from _polaris_fake import ROOT, STS, Cluster, FakePolaris, serve

from olf.common.tooling.kubectl import Kubectl
from olf.infra import k8s
from olf.infra.polaris import PolarisClient, PolarisConfig, PolarisError
from olf.infra.polaris_bootstrap import (
    DROP_WITH_PURGE,
    CatalogSts,
    Credential,
    StageCatalog,
    ensure_catalog,
    ensure_credential,
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
