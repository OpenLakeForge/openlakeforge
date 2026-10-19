from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from olf import k8s
from olf.clients.openmetadata import OpenMetadataError
from olf.openmetadata import _sso

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

    def __init__(self, _url: str, token: str) -> None:
        assert token == "jwt"

    def request(self, method: str, path: str, json_body: Any = None, ok_statuses: Any = None) -> dict[str, str]:
        FakeClient.calls.append((method, path, json_body))
        return FakeClient.current if method == "GET" else {}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"data": DATA, "probe": [True], "restarts": 0}
    FakeClient.calls, FakeClient.current = [], {}

    @contextmanager
    def service_url(*_a: Any, **_k: Any):  # noqa: ANN202
        yield "http://om"

    def restart(*_a: Any) -> None:
        state["restarts"] += 1

    monkeypatch.setattr(_sso, "OpenMetadataClient", FakeClient)
    monkeypatch.setattr(_sso.access, "service_url", service_url)
    monkeypatch.setattr(_sso.k8s, "configmap_data", lambda *_a: state["data"])
    monkeypatch.setattr(_sso, "automation_token", lambda *_a: "jwt")
    monkeypatch.setattr(_sso.k8s, "secret_value", lambda *_a: "client-secret")
    monkeypatch.setattr(_sso.k8s, "exec_succeeds", lambda *_a: state["probe"].pop(0) if state["probe"] else False)
    monkeypatch.setattr(_sso.k8s, "restart_deployment", restart)
    monkeypatch.setattr(_sso.time, "sleep", lambda _s: None)
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
    assert [c[0] for c in FakeClient.calls] == ["GET", "PUT"]
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
    monkeypatch.setattr(_sso, "automation_token", lambda *_a: None)
    with pytest.raises(OpenMetadataError, match="automation bot token"):
        run()


def test_configmap_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "_kubectl", lambda *_a, **_k: "")
    assert k8s.configmap_data("c", "ns") is None
    monkeypatch.setattr(k8s, "_kubectl", lambda *_a, **_k: '{"data": {"a": "b"}}')
    assert k8s.configmap_data("c", "ns") == {"a": "b"}
