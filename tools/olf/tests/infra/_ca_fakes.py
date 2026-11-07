"""A stateful stand-in for what `local-ca` signs and publishes (#929): the publish Job, the trust ConfigMap, the
Certificates and their Secrets, and the workloads that mount them. It models only the rules a restore depends on:
Terraform runs the publish Job only when it is missing, and cert-manager signs a Certificate only when its Secret is
missing."""

from __future__ import annotations

from typing import Any

import pytest

from olf.infra import k8s
from olf.infra.backup._restore import CA_CONFIGMAP, LOCAL_CA_ISSUER

PUBLISH_JOB = "publish-local-ca-0a1b2c3d4e"
SHARED = "olf-system"


class FakeCaTrust:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        secrets: dict[tuple[str, str], tuple[str, dict[str, str]]],
        replicas: dict[tuple[str, str], int],
    ) -> None:
        self.secrets = secrets
        self.replicas = replicas
        self.jobs: set[str] = set()
        self.trust: dict[str, str] = {}  # ConfigMap name -> the CA it holds
        self.certificates: dict[tuple[str, str], tuple[str, str]] = {}  # (ns, name) -> (secret name, issuer)
        self.mounts: dict[tuple[str, str], set[tuple[str, str]]] = {}  # (ns, workload) -> {(kind, name)}
        self.restarted: list[tuple[str, str]] = []
        self.restored_when_restarted: dict[tuple[str, str], bool] = {}
        self.cert_manager_down = False
        monkeypatch.setattr(k8s, "delete_jobs", self.delete_jobs)
        monkeypatch.setattr(k8s, "certificates_issued_by", self.certificates_issued_by)
        monkeypatch.setattr(k8s, "wait_for_certificate", self.wait_for_certificate)
        monkeypatch.setattr(k8s, "workloads_reading", self.workloads_reading)
        monkeypatch.setattr(k8s, "restart_workload", self.restart_workload)

    @property
    def ca(self) -> str:
        return self.secrets[(SHARED, "local-ca")][1]["tls.crt"]

    def deploy_with(self, ca: str) -> None:
        """The state a fresh deploy leaves: the CA it minted, published and used to sign every Certificate."""
        self.secrets[(SHARED, "local-ca")] = ("kubernetes.io/tls", {"tls.crt": ca})
        self.jobs = {PUBLISH_JOB}
        self.trust = {CA_CONFIGMAP: ca}
        self.cert_manager()

    def add_certificate(self, namespace: str, name: str, secret: str, *, issuer: str = LOCAL_CA_ISSUER) -> None:
        self.certificates[(namespace, name)] = (secret, issuer)

    def signer(self, namespace: str, secret: str) -> str | None:
        found = self.secrets.get((namespace, secret))
        return found[1]["signed-by"] if found else None

    def cert_manager(self) -> None:
        if self.cert_manager_down:
            return
        for (namespace, _name), (secret, issuer) in self.certificates.items():
            if (namespace, secret) not in self.secrets:
                self.secrets[(namespace, secret)] = (
                    "kubernetes.io/tls",
                    {"signed-by": self.ca if issuer == LOCAL_CA_ISSUER else issuer},
                )

    def reconcile(self) -> None:
        """`olf infra deploy`: the publish Job runs only when it is gone; cert-manager keeps working throughout."""
        if PUBLISH_JOB not in self.jobs:
            self.jobs.add(PUBLISH_JOB)
            self.trust = {CA_CONFIGMAP: self.ca}
        self.cert_manager()

    def delete_jobs(self, selector: str, namespace: str, **_: Any) -> None:
        assert (selector, namespace) == ("app.kubernetes.io/name=in-cluster-resolution", SHARED)
        self.jobs.clear()

    def certificates_issued_by(self, issuer: str, **_: Any) -> list[tuple[str, str, str]]:
        return [(ns, name, secret) for (ns, name), (secret, by) in self.certificates.items() if by == issuer]

    def wait_for_certificate(self, name: str, namespace: str, secret: str, **_: Any) -> None:
        self.cert_manager()
        if (namespace, secret) not in self.secrets:
            raise k8s.KubectlError(f"cert-manager did not re-issue Secret {namespace}/{secret} within 300.0s")

    def workloads_reading(
        self, namespaces: Any, *, configmaps: Any, secrets: Any, **_: Any
    ) -> list[tuple[str, str, str]]:
        found = []
        for (namespace, name), mounted in self.mounts.items():
            reads = any(
                (kind == "configmap" and ref in configmaps) or (kind == "secret" and (namespace, ref) in secrets)
                for kind, ref in mounted
            )
            if reads and namespace in namespaces and self.replicas.get((namespace, name)):
                found.append(("Deployment", name, namespace))
        return found

    def restart_workload(self, kind: str, name: str, namespace: str, **_: Any) -> None:
        assert kind == "Deployment"
        self.restarted.append((namespace, name))
        # A pod that starts now loads the CA and the Secrets as they are: they must all be the restored ones.
        self.restored_when_restarted[(namespace, name)] = self.trust[CA_CONFIGMAP] == self.ca and all(
            self.signer(ns, secret) == self.ca
            for (ns, _n), (secret, issuer) in self.certificates.items()
            if issuer == LOCAL_CA_ISSUER
        )
