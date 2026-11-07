from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import requests

ROOT = Path(__file__).resolve().parents[4]


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / f"images/superset/{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


olf_trino = _load("olf_trino")
olf_oidc = _load("olf_oidc")


class _Url:
    """The slice of sqlalchemy's immutable URL the mutator uses."""

    def __init__(self, backend: str = "trino", password: str | None = None) -> None:
        self.backend, self.password = backend, password

    def get_backend_name(self) -> str:
        return self.backend

    def set(self, password: str | None) -> _Url:
        return _Url(self.backend, password)


class _Security:
    def find_user(self, username: str) -> Any:
        return types.SimpleNamespace(email=f"{username}@acme.org") if username == "oidc-ada" else None


def _params(token: str | None = "tok", **args: Any) -> dict[str, Any]:
    connect_args = dict(args)
    if token:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {token}"
        connect_args["http_session"] = session
    return {"connect_args": connect_args}


def _mutate(uri: _Url, params: dict[str, Any], username: str | None = "oidc-ada") -> tuple[_Url, dict[str, Any]]:
    return olf_trino.db_connection_mutator(uri, params, username, _Security(), "chart")


def test_the_bearer_travels_as_the_token_extra_credential_for_the_email_user() -> None:
    uri, params = _mutate(_Url(), _params("abc"))

    assert params["connect_args"]["extra_credential"] == [("token", "abc")]
    assert params["connect_args"]["user"] == "oidc-ada@acme.org"
    assert uri.password is None


def test_a_uri_password_is_stripped_so_it_cannot_replace_the_bearer() -> None:
    uri, _ = _mutate(_Url(password="secret"), _params())

    assert uri.password is None


def test_verify_reaches_the_supplied_session() -> None:
    _, params = _mutate(_Url(), _params(verify="/ca.pem"))

    assert params["connect_args"]["http_session"].verify == "/ca.pem"


def test_a_user_without_an_email_keeps_the_superset_username() -> None:
    _, params = _mutate(_Url(), _params(), username="oidc-bob")

    assert params["connect_args"]["user"] == "oidc-bob"


@pytest.mark.parametrize("backend", ["trino", "postgresql"])
def test_without_a_bearer_nothing_changes_and_no_identity_is_invented(backend: str) -> None:
    uri = _Url(backend, password="kept")
    params = _params(token=None)

    out_uri, out_params = _mutate(uri, params)

    assert out_uri is uri
    assert out_params == {"connect_args": {}}


def test_another_engine_is_left_alone_even_with_a_bearer() -> None:
    _, params = _mutate(_Url("postgresql"), _params())

    assert "extra_credential" not in params["connect_args"]


class _Dex:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status, self.body = status, body or {}

    def post(self, *_a: Any, **_k: Any) -> requests.Response:
        response = requests.Response()
        response.status_code = self.status
        response.json = lambda: self.body  # type: ignore[method-assign]
        return response


@pytest.fixture
def engine_spec(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Superset 6.1.0's refresh, with Dex replaced by the test's `dex`."""

    class TrinoAuthError(Exception):
        pass

    class TrinoEngineSpec:
        dex: _Dex

        @classmethod
        def get_oauth2_fresh_token(cls, config: dict, refresh_token: str) -> dict:
            response = cls.dex.post(config["token_request_uri"])
            response.raise_for_status()
            return response.json()

    modules = {
        "superset": types.ModuleType("superset"),
        "superset.db_engine_specs": types.ModuleType("superset.db_engine_specs"),
        "superset.db_engine_specs.trino": types.ModuleType("superset.db_engine_specs.trino"),
        "trino": types.ModuleType("trino"),
        "trino.exceptions": types.ModuleType("trino.exceptions"),
    }
    modules["superset.db_engine_specs.trino"].TrinoEngineSpec = TrinoEngineSpec  # type: ignore[attr-defined]
    modules["trino.exceptions"].TrinoAuthError = TrinoAuthError  # type: ignore[attr-defined]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    olf_trino.install()
    TrinoEngineSpec.auth_error = TrinoAuthError  # type: ignore[attr-defined]
    return TrinoEngineSpec


def test_a_refreshed_token_expires_a_minute_early(engine_spec: Any) -> None:
    engine_spec.dex = _Dex(200, {"access_token": "new", "expires_in": 300})

    assert engine_spec.get_oauth2_fresh_token({"token_request_uri": "u"}, "r")["expires_in"] == 240


@pytest.mark.parametrize("status", [400, 401])
def test_a_refused_refresh_token_is_an_auth_error_so_superset_forgets_it(engine_spec: Any, status: int) -> None:
    engine_spec.dex = _Dex(status, {"error": "invalid_grant"})

    with pytest.raises(engine_spec.auth_error):
        engine_spec.get_oauth2_fresh_token({"token_request_uri": "u"}, "r")


def test_an_issuer_outage_is_not_mistaken_for_a_dead_refresh_token(engine_spec: Any) -> None:
    engine_spec.dex = _Dex(503)

    with pytest.raises(requests.HTTPError):
        engine_spec.get_oauth2_fresh_token({"token_request_uri": "u"}, "r")


def test_the_trino_sign_in_reuses_the_superset_client_and_keeps_shared_caches_per_user() -> None:
    cfg = {
        "issuer_url": "https://auth.olf.localhost/",
        "client_id": "superset",
        "database_redirect_uri": "https://superset-prod.olf.localhost/api/v1/database/oauth2/",
        "people_catalog": "prod",
    }

    out = olf_oidc.trino_settings(cfg, "s")
    client = out["DATABASE_OAUTH2_CLIENTS"]["Trino"]

    assert (client["id"], client["secret"]) == ("superset", "s")
    assert client["token_request_uri"] == "https://auth.olf.localhost/token"
    assert "audience:server:client_id:olf-cli" in client["scope"].split()
    assert out["FEATURE_FLAGS"] == {
        "CACHE_QUERY_BY_USER": True,
        "CACHE_IMPERSONATION": True,
        "ALERT_REPORTS": False,
        "THUMBNAILS": False,
    }
    assert "line" in out["VIZ_TYPE_DENYLIST"]
    assert out["OLF_ICEBERG_CATALOGS"] == ["iceberg", "prod"]


def test_the_image_pins_the_trino_client_and_ships_the_module() -> None:
    dockerfile = (ROOT / "images/superset/Dockerfile").read_text()

    assert '"trino==' in dockerfile
    assert "olf_trino.py" in dockerfile
