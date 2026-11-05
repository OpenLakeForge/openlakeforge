from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from openlakeforge_services.openmetadata import OpenMetadataError

from olf.infra import k8s
from olf.infra.openmetadata import _sso

AUTH = {
    "provider": "custom-oidc",
    "authority": "https://auth.olf.localhost",
    "clientId": "openmetadata",
    "callbackUrl": "https://om/callback",
}
CONFIG = {"authenticationConfiguration": {**AUTH, "oidcConfiguration": {"discoveryUri": "https://auth/.well-known"}}}
DATA = {"config.json": json.dumps(CONFIG), "secret_name": "s", "secret_key": "k"}


class FakeClient:
    calls: list[tuple[str, str, Any]] = []
    current: dict[str, str] = {}
    admin: dict[str, bool] = {"id": "u1", "isAdmin": True}

    def __init__(self, _url: str, token: str) -> None:
        assert token == "jwt"

    def request(self, method: str, path: str, json_body: Any = None, ok_statuses: Any = None, **_k: Any) -> Any:
        FakeClient.calls.append((method, path, json_body))
        if path == "/api/v1/users/name/admin" and method == "GET":
            return FakeClient.admin
        return FakeClient.current if method == "GET" else {}


class FakeRoute:
    def client(self, _cls: Any, **kwargs: Any) -> FakeClient:
        return FakeClient("http://om", **kwargs)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"data": DATA, "probe": [True], "restarts": 0}
    FakeClient.calls, FakeClient.current, FakeClient.admin = [], {}, {"id": "u1", "isAdmin": True}

    @contextmanager
    def service_route(*_a: Any, **_k: Any):  # noqa: ANN202
        yield FakeRoute()

    def restart(*_a: Any, **_k: Any) -> None:
        state["restarts"] += 1

    monkeypatch.setattr(_sso.access, "service_route", service_route)
    monkeypatch.setattr(_sso.k8s, "configmap_data", lambda *_a, **_k: state["data"])
    monkeypatch.setattr(_sso, "automation_token", lambda *_a, **_k: "jwt")
    monkeypatch.setattr(_sso.k8s, "secret_value", lambda *_a, **_k: "client-secret")
    monkeypatch.setattr(_sso.k8s, "exec_succeeds", lambda *_a, **_k: state["probe"].pop(0) if state["probe"] else False)
    monkeypatch.setattr(_sso.k8s, "restart_deployment", restart)
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(_sso.time, "sleep", lambda _s: None)
    monkeypatch.setattr(_sso.time, "monotonic", lambda: float(next(clock)))
    return state


def run() -> None:
    _sso.apply_sso("ns", service="openmetadata", remote_port=8585, log_path="/dev/null")


def test_puts_the_config_with_the_secret_then_restarts(env: dict[str, Any]) -> None:
    run()
    put = [c for c in FakeClient.calls if c[0] == "PUT"]
    assert put[0][1] == "/api/v1/system/security/config"
    assert put[0][2]["authenticationConfiguration"]["oidcConfiguration"]["secret"] == "client-secret"
    assert env["restarts"] == 1


def test_an_applied_config_is_reput_without_a_restart(env: dict[str, Any]) -> None:
    FakeClient.current = dict(AUTH)
    run()
    assert [c[0] for c in FakeClient.calls][:2] == ["GET", "PUT"]
    assert env["restarts"] == 0


def test_no_oidc_means_no_put(env: dict[str, Any]) -> None:
    env["data"] = None
    run()
    assert FakeClient.calls == []
    assert env["restarts"] == 0


def test_an_issuer_that_never_answers_fails_before_the_put(env: dict[str, Any]) -> None:
    env["probe"] = []
    with pytest.raises(OpenMetadataError, match="did not answer from inside the cluster"):
        run()
    assert [c[0] for c in FakeClient.calls] == ["GET"]
    assert env["restarts"] == 0


def test_a_missing_automation_token_is_an_error(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sso, "automation_token", lambda *_a, **_k: None)
    with pytest.raises(OpenMetadataError, match="automation bot token"):
        run()


def test_configmap_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "_kubectl", lambda *_a, **_k: "")
    assert k8s.configmap_data("c", "ns") is None
    monkeypatch.setattr(k8s, "_kubectl", lambda *_a, **_k: '{"data": {"a": "b"}}')
    assert k8s.configmap_data("c", "ns") == {"a": "b"}


def test_the_local_admin_is_demoted_once(env: dict[str, Any]) -> None:
    run()
    patch = [c for c in FakeClient.calls if c[0] == "PATCH"]
    assert patch == [("PATCH", "/api/v1/users/u1", [{"op": "add", "path": "/isAdmin", "value": False}])]
    FakeClient.calls.clear()
    FakeClient.admin = {"id": "u1", "isAdmin": False}
    env["probe"] = [True]
    run()
    assert not [c for c in FakeClient.calls if c[0] == "PATCH"]


def test_apply_sso_reads_the_cluster_through_the_explicit_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """kubectl answers an absent ConfigMap with nothing; the process env names no context, the mapping does."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text("#!/bin/sh\n")
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    for name in ("KUBE_CONTEXT", "OLF_TOOLCHAIN_MODE"):
        monkeypatch.delenv(name, raising=False)
    environ = {"KUBE_CONTEXT": "explicit-ctx", "OLF_TOOLCHAIN_MODE": "host"}

    _sso.apply_sso("ns", service="openmetadata", remote_port=8585, log_path="/dev/null", environ=environ)

    with pytest.raises(k8s.KubectlError, match="KUBE_CONTEXT is required"):
        _sso.apply_sso("ns", service="openmetadata", remote_port=8585, log_path="/dev/null")
