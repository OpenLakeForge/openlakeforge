import io
import json
import os
import urllib.parse
from typing import Any

import pytest
from openlakeforge_runtime import polaris_token

ENV = {
    "OPENLAKEFORGE_CATALOG_TOKEN_URI": "http://polaris/oauth/tokens",
    "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY": "POLARIS_PROCESSING_CLIENT_ID",
    "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY": "POLARIS_PROCESSING_CLIENT_SECRET",
    "POLARIS_PROCESSING_CLIENT_ID": "id",
    "POLARIS_PROCESSING_CLIENT_SECRET": "secret",
}


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []

    def urlopen(request: Any, timeout: float) -> io.StringIO:
        sent.append(request)
        return io.StringIO(json.dumps({"access_token": f"token-{len(sent)}"}))

    monkeypatch.setattr(polaris_token.urllib.request, "urlopen", urlopen)
    return sent


def test_mint_posts_the_stage_principal_credentials(requests: list[Any]) -> None:
    assert polaris_token.mint_token(ENV) == "token-1"
    form = urllib.parse.parse_qs(requests[0].data.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["id"]
    assert form["client_secret"] == ["secret"]


def test_no_token_endpoint_means_no_token(requests: list[Any]) -> None:
    assert polaris_token.mint_token({}) == ""
    assert requests == []


def test_each_invocation_mints_once_and_restores_the_environment(
    monkeypatch: pytest.MonkeyPatch, requests: list[Any]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(polaris_token.TOKEN_ENV, raising=False)

    with polaris_token.polaris_token_env():
        assert os.environ[polaris_token.TOKEN_ENV] == "token-1"
    assert polaris_token.TOKEN_ENV not in os.environ
    with polaris_token.polaris_token_env():
        assert os.environ[polaris_token.TOKEN_ENV] == "token-2"
    assert len(requests) == 2
