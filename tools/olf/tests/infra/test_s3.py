from __future__ import annotations

import os
from pathlib import Path

import pytest

from olf.infra import k8s, s3


def test_port_forward_client_reads_the_log_prefix_and_kube_env_from_the_given_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text('#!/bin/sh\necho "kc=$KUBECONFIG"\n')  # exits at once: the forward never binds
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", str(tmp_path / "from-os-environ"))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    environ = {
        "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX": str(tmp_path / "from-given"),
        "KUBE_CONTEXT": "ctx",
        "KUBECONFIG": "/given/kubeconfig",
        "OLF_TOOLCHAIN_MODE": "host",
    }

    with pytest.raises(k8s.KubectlError, match="kc=/given/kubeconfig"):
        with s3.port_forward_client(
            "bucket",
            service="svc",
            remote_port=1,
            namespace="ns",
            access_key_id="a",
            secret_access_key="k",
            region="r",
            environ=environ,
        ):
            pass

    assert (tmp_path / "from-given-seaweedfs-port-forward.log").exists()
    assert not (tmp_path / "from-os-environ-seaweedfs-port-forward.log").exists()
