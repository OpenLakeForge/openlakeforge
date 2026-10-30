from __future__ import annotations

import json
import urllib.error

import pytest

from olf.infra import polaris
from olf.infra.catalog import NamespaceState


class FakeResponse:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_list_namespaces_skips_nested_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    config = polaris.PolarisConfig(
        base_url="http://127.0.0.1:8181",
        catalog_name="lakehouse_dev",
        client_id="id",
        client_secret="secret",
    )
    client = polaris.PolarisClient(config, token="t")

    def fake_urlopen(request, timeout=0):  # noqa: ANN001, ARG001 - urllib signature
        if request.full_url.endswith("/namespaces"):
            return FakeResponse({"namespaces": [["sales_silver"], ["sales", "nested"]]})
        return FakeResponse({"properties": {"location": "s3://silver/sales_silver/"}})

    monkeypatch.setattr(polaris.urllib.request, "urlopen", fake_urlopen)

    assert client.list_namespaces() == {
        "sales_silver": NamespaceState("s3://silver/sales_silver/", {"location": "s3://silver/sales_silver/"})
    }


def test_login_retries_until_polaris_accepts_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    client = polaris.PolarisClient(
        polaris.PolarisConfig(
            base_url="http://127.0.0.1:8181",
            catalog_name="lakehouse_dev",
            client_id="id",
            client_secret="secret",
        )
    )
    attempts = 0
    delays: list[int] = []

    def fake_urlopen(request, timeout=0):  # noqa: ANN001, ARG001 - urllib signature
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.URLError(ConnectionRefusedError("Polaris is starting"))
        return FakeResponse({"access_token": "token"})

    monkeypatch.setattr(polaris.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(polaris.time, "sleep", delays.append)

    client.login()

    assert client.token == "token"
    assert attempts == 3
    assert delays == [polaris.TOKEN_REQUEST_RETRY_DELAY_SECONDS] * 2


def test_login_fails_after_retry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = polaris.PolarisClient(
        polaris.PolarisConfig(
            base_url="http://127.0.0.1:8181",
            catalog_name="lakehouse_dev",
            client_id="id",
            client_secret="secret",
        )
    )

    def fake_urlopen(request, timeout=0):  # noqa: ANN001, ARG001 - urllib signature
        raise urllib.error.URLError(ConnectionRefusedError("Polaris is starting"))

    monkeypatch.setattr(polaris, "TOKEN_REQUEST_ATTEMPTS", 2)
    monkeypatch.setattr(polaris.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(polaris.time, "sleep", lambda delay: None)

    with pytest.raises(polaris.PolarisError, match="failed after 2 attempts"):
        client.login()


def test_a_rejected_token_is_replaced_once_and_the_request_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    config = polaris.PolarisConfig(
        base_url="http://127.0.0.1:8181", catalog_name="lakehouse_dev", client_id="id", client_secret="secret"
    )
    client = polaris.PolarisClient(config, token="minted-before-the-clock-stepped-back")
    seen: list[str | None] = []

    def fake_urlopen(request, timeout=0):  # noqa: ANN001, ARG001 - urllib signature
        if request.full_url.endswith("/oauth/tokens"):
            return FakeResponse({"access_token": "fresh"})
        seen.append(request.get_header("Authorization"))
        if len(seen) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        return FakeResponse({"properties": {"location": "s3://silver/sales_silver/"}})

    monkeypatch.setattr(polaris.urllib.request, "urlopen", fake_urlopen)

    assert client.namespace_state("sales_silver").location == "s3://silver/sales_silver/"
    assert seen == ["Bearer minted-before-the-clock-stepped-back", "Bearer fresh"]


def test_a_token_rejected_again_after_relogin_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    config = polaris.PolarisConfig(
        base_url="http://127.0.0.1:8181", catalog_name="lakehouse_dev", client_id="id", client_secret="secret"
    )
    client = polaris.PolarisClient(config, token="t")
    logins = 0

    def fake_urlopen(request, timeout=0):  # noqa: ANN001, ARG001 - urllib signature
        nonlocal logins
        if request.full_url.endswith("/oauth/tokens"):
            logins += 1
            return FakeResponse({"access_token": "fresh"})
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(polaris.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(polaris.PolarisError, match="HTTP 401"):
        client.namespace_state("sales_silver")
    assert logins == 1
