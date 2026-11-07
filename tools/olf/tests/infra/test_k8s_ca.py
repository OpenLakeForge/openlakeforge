"""Finding what a restored `local-ca` touches (#929), against a kubectl that serves the objects it is asked for."""

from __future__ import annotations

import json
from typing import Any

import pytest

from olf.infra import k8s


def _certificate(namespace: str, name: str, kind: str = "ClusterIssuer", issuer: str = "local-ca") -> dict[str, Any]:
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"secretName": f"{name}-tls", "issuerRef": {"name": issuer, "kind": kind}},
    }


def _workload(kind: str, name: str, volumes: list[dict[str, Any]], replicas: int = 1) -> dict[str, Any]:
    return {
        "kind": kind,
        "metadata": {"name": name},
        "spec": {"replicas": replicas, "template": {"spec": {"volumes": volumes}}},
    }


class FakeKubectl:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.certificates: list[dict[str, Any]] = []
        self.workloads: dict[str, list[dict[str, Any]]] = {}
        self.secrets: set[str] = set()
        self.calls: list[list[str]] = []
        self.secret_appears_after = 0  # listings before the Secret exists
        monkeypatch.setattr(k8s, "_kubectl", self.kubectl)
        monkeypatch.setattr(k8s.time, "sleep", lambda _seconds: None)

    def kubectl(self, args: list[str], **_: Any) -> str:
        self.calls.append(args)
        if args[:2] == ["get", "certificates"]:
            return json.dumps({"items": self.certificates})
        if args[:2] == ["get", "deployments,statefulsets"]:
            return json.dumps({"items": self.workloads.get(args[args.index("-n") + 1], [])})
        if args[:2] == ["get", "secret"]:
            if self.secret_appears_after > 0:
                self.secret_appears_after -= 1
                return ""
            return json.dumps({"data": {}}) if args[2] in self.secrets else ""
        return ""


@pytest.fixture
def kubectl(monkeypatch: pytest.MonkeyPatch) -> FakeKubectl:
    return FakeKubectl(monkeypatch)


def test_only_certificates_of_the_cluster_issuer_are_found_in_every_namespace(kubectl: FakeKubectl) -> None:
    kubectl.certificates = [
        _certificate("olf-system", "trino-tls"),
        _certificate("olf-dev", "route"),
        _certificate("olf-system", "other-issuer", issuer="letsencrypt"),
        _certificate("olf-system", "namespaced-issuer", kind="Issuer"),
    ]

    assert k8s.certificates_issued_by("local-ca") == [
        ("olf-system", "trino-tls", "trino-tls-tls"),
        ("olf-dev", "route", "route-tls"),
    ]


def test_workloads_that_mount_the_ca_or_a_listed_secret_are_found_unless_they_are_stopped(
    kubectl: FakeKubectl,
) -> None:
    ca = {"name": "ca", "configMap": {"name": "olf-local-ca"}}
    tls = {"name": "tls", "secret": {"secretName": "trino-tls"}}
    projected = {"name": "p", "projected": {"sources": [{"secret": {"name": "trino-tls"}}]}}
    kubectl.workloads = {
        "olf-system": [
            _workload("Deployment", "polaris", [ca]),
            _workload("StatefulSet", "trino", [tls]),
            _workload("Deployment", "bundle", [projected]),
            _workload("Deployment", "stopped", [ca], replicas=0),
            _workload("Deployment", "unrelated", [{"name": "x", "configMap": {"name": "other"}}]),
            _workload("Deployment", "no-volumes", []),
        ],
        "olf-dev": [_workload("Deployment", "same-name-other-namespace", [tls])],
    }

    found = k8s.workloads_reading(
        ["olf-system", "olf-dev", "olf-system"], configmaps={"olf-local-ca"}, secrets={("olf-system", "trino-tls")}
    )

    assert found == [
        ("Deployment", "polaris", "olf-system"),
        ("StatefulSet", "trino", "olf-system"),
        ("Deployment", "bundle", "olf-system"),
    ]


def test_a_certificate_is_awaited_until_its_secret_exists_and_it_is_ready(kubectl: FakeKubectl) -> None:
    kubectl.secrets = {"trino-tls"}
    kubectl.secret_appears_after = 2

    k8s.wait_for_certificate("trino-tls", "olf-system", "trino-tls")

    assert kubectl.secret_appears_after == 0
    assert kubectl.calls[-1][:3] == ["wait", "--for=condition=Ready", "certificate/trino-tls"]


def test_a_certificate_whose_secret_never_appears_fails_clearly(
    kubectl: FakeKubectl, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter(range(0, 1000, 100))
    monkeypatch.setattr(k8s.time, "monotonic", lambda: float(next(ticks)))

    with pytest.raises(k8s.KubectlError, match=r"did not re-issue Secret olf-system/trino-tls within 300.0s"):
        k8s.wait_for_certificate("trino-tls", "olf-system", "trino-tls")
