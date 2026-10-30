"""Token validation, driven through /api/v1/whoami: the API decides identity from the token alone."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from conftest import AUDIENCE, KID, PRECEDENCE, ROLE_MAPPING, FakeIssuer, Mint, bearer
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings, SettingsError
from openlakeforge_platform_api.tokens import TokenValidator

WHOAMI = "/api/v1/whoami"


@pytest.mark.parametrize("role", PRECEDENCE)
def test_a_valid_token_for_each_canonical_role_is_accepted(client: TestClient, mint: Mint, role: str) -> None:
    response = client.get(WHOAMI, headers=bearer(mint(groups=list(ROLE_MAPPING[role]))))

    assert response.status_code == 200
    assert response.json() == {
        "subject": "user-1",
        "role": role,
        "email": "ada@example.com",
        "name": "ada",
        "audience": AUDIENCE,
    }


def test_several_roles_resolve_to_the_highest(client: TestClient, mint: Mint) -> None:
    token = mint(groups=["acme:viewers", "acme:data-engineers", "acme:analysts"])

    assert client.get(WHOAMI, headers=bearer(token)).json()["role"] == "data-engineer"


def test_the_role_claim_is_the_one_settings_name(issuer: FakeIssuer, settings: Settings) -> None:
    custom = Settings.from_mapping({**_raw(settings), "role_claim": "cognito:groups"})
    client = TestClient(create_app(custom))

    token = issuer.mint(groups=["acme:platform-admins"], **{"cognito:groups": "acme:analysts"})

    assert client.get(WHOAMI, headers=bearer(token)).json()["role"] == "analyst"


def test_an_absent_token_is_refused(client: TestClient) -> None:
    response = client.get(WHOAMI)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "not-a-scheme"])
def test_an_authorization_header_without_a_bearer_token_is_refused(client: TestClient, header: str) -> None:
    assert client.get(WHOAMI, headers={"Authorization": header}).status_code == 401


def test_an_expired_token_is_refused(client: TestClient, mint: Mint) -> None:
    now = int(time.time())

    assert client.get(WHOAMI, headers=bearer(mint(iat=now - 600, exp=now - 60))).status_code == 401


def test_a_token_with_a_tampered_payload_is_refused(client: TestClient, mint: Mint) -> None:
    header, payload, signature = mint().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["groups"] = ["acme:platform-admins"]

    assert client.get(WHOAMI, headers=bearer(f"{header}.{_b64(claims)}.{signature}")).status_code == 401


def test_a_token_signed_by_another_key_under_the_issuers_kid_is_refused(client: TestClient, mint: Mint) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    assert client.get(WHOAMI, headers=bearer(mint(key=other))).status_code == 401


def test_a_token_naming_an_unknown_key_is_refused(client: TestClient, mint: Mint) -> None:
    assert client.get(WHOAMI, headers=bearer(mint(kid="someone-elses-key"))).status_code == 401


def test_an_unsigned_token_is_refused(client: TestClient) -> None:
    now = int(time.time())
    claims = {"iss": "x", "aud": AUDIENCE, "sub": "s", "iat": now, "exp": now + 60, "groups": ["acme:viewers"]}

    assert client.get(WHOAMI, headers=bearer(f"{_b64({'alg': 'none', 'kid': KID})}.{_b64(claims)}.")).status_code == 401


def test_a_token_signed_with_the_public_key_as_an_hmac_secret_is_refused(
    client: TestClient, issuer: FakeIssuer
) -> None:
    # The algorithm-confusion forgery: anyone can read the public key. PyJWT
    # will not sign this, so the token is assembled by hand.
    secret = issuer.key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    now = int(time.time())
    claims = {"iss": issuer.url, "aud": AUDIENCE, "sub": "s", "iat": now, "exp": now + 60, "groups": ["acme:viewers"]}
    signing_input = f"{_b64({'alg': 'HS256', 'kid': KID, 'typ': 'JWT'})}.{_b64(claims)}"
    signature = base64.urlsafe_b64encode(hmac.new(secret, signing_input.encode(), hashlib.sha256).digest())

    assert client.get(WHOAMI, headers=bearer(f"{signing_input}.{signature.rstrip(b'=').decode()}")).status_code == 401


def test_a_malformed_token_is_refused(client: TestClient) -> None:
    assert client.get(WHOAMI, headers=bearer("not.a.jwt")).status_code == 401


def test_a_token_for_another_audience_is_refused(client: TestClient, mint: Mint) -> None:
    # A Superset client token reaching the API is not a token issued for it.
    assert client.get(WHOAMI, headers=bearer(mint(aud="superset"))).status_code == 401


def test_a_token_with_several_audiences_is_accepted_when_one_is_listed(client: TestClient, mint: Mint) -> None:
    response = client.get(WHOAMI, headers=bearer(mint(aud=["superset", AUDIENCE])))

    assert response.status_code == 200
    assert response.json()["audience"] == AUDIENCE


def test_a_token_from_another_issuer_is_refused(client: TestClient, mint: Mint) -> None:
    assert client.get(WHOAMI, headers=bearer(mint(iss="https://auth.elsewhere.example"))).status_code == 401


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "aud", "iss"])
def test_a_token_missing_a_required_claim_is_refused(client: TestClient, mint: Mint, claim: str) -> None:
    assert client.get(WHOAMI, headers=bearer(mint(**{claim: None}))).status_code == 401


def test_a_valid_token_with_no_recognised_role_is_denied(client: TestClient, mint: Mint) -> None:
    response = client.get(WHOAMI, headers=bearer(mint(groups=["acme:contractors"])))

    assert response.status_code == 403
    assert "no OpenLakeForge role" in response.json()["detail"]


def test_perimeter_identity_headers_alone_are_not_identity(client: TestClient) -> None:
    headers = {
        "X-Auth-Request-User": "admin",
        "X-Auth-Request-Email": "admin@example.com",
        "X-Auth-Request-Groups": "acme:platform-admins",
        "X-Forwarded-Groups": "acme:platform-admins",
    }

    assert client.get(WHOAMI, headers=headers).status_code == 401


def test_perimeter_identity_headers_do_not_change_the_tokens_role(client: TestClient, mint: Mint) -> None:
    headers = {**bearer(mint(groups=["acme:viewers"])), "X-Auth-Request-Groups": "acme:platform-admins"}

    assert client.get(WHOAMI, headers=headers).json()["role"] == "viewer"


def test_an_unreachable_issuer_is_unavailable_not_accepted(client: TestClient, issuer: FakeIssuer, mint: Mint) -> None:
    issuer.down = True
    assert client.get(WHOAMI, headers=bearer(mint())).status_code == 503

    issuer.down = False
    assert client.get(WHOAMI, headers=bearer(mint())).status_code == 200


def test_a_discovery_document_naming_another_issuer_is_not_trusted(
    client: TestClient, issuer: FakeIssuer, mint: Mint
) -> None:
    issuer.advertised = "https://auth.elsewhere.example"

    assert client.get(WHOAMI, headers=bearer(mint())).status_code == 503


def test_a_signing_key_the_issuer_withdraws_stops_validating_once_the_key_set_expires(
    issuer: FakeIssuer, settings: Settings, mint: Mint
) -> None:
    client = TestClient(create_app(settings, validator=TokenValidator(settings, jwks_lifespan=0.05)))
    assert client.get(WHOAMI, headers=bearer(mint())).status_code == 200

    issuer.publishes_key = False
    time.sleep(0.1)

    assert client.get(WHOAMI, headers=bearer(mint())).status_code == 401


def test_healthz_needs_no_token_and_does_not_call_the_issuer(client: TestClient, issuer: FakeIssuer) -> None:
    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert issuer.requests == 0


@pytest.mark.parametrize(
    "change",
    [
        {"audiences": []},
        {"audiences": [""]},
        {"issuer_url": ""},
        {"precedence": ["viewer", "viewer"]},
        {"role_mapping": {"owner": ["acme:owners"]}},
    ],
)
def test_settings_that_would_weaken_validation_refuse_to_load(settings: Settings, change: dict[str, object]) -> None:
    with pytest.raises(SettingsError):
        Settings.from_mapping({**_raw(settings), **change})


def _b64(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def _raw(settings: Settings) -> dict[str, object]:
    return {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": list(settings.stages),
    }
