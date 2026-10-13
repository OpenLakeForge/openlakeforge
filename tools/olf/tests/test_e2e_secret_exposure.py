from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from olf import k8s
from olf.e2e import _secret_exposure as exposure
from olf.e2e._shell import E2EError

ADMIN = "A" * 32
CLIENT = "c1ientSecret0123456789abcdefXYZ"
CREDENTIALS = {"keycloak-admin-creds/password": ADMIN, "keycloak-client-perimeter/client-secret": CLIENT}


def test_clean_surfaces_pass_and_an_unreadable_one_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(exposure.log, "info", messages.append)

    exposure.scan(CREDENTIALS, {"ConfigMaps in olf-system": "{}", "logs of pod/x": None}, link_surfaces={})

    assert messages == ["Skipping the exposure scan of logs of pod/x: unavailable."]


@pytest.mark.parametrize("leak", [CLIENT, base64.b64encode(CLIENT.encode()).decode()])
def test_a_leaking_surface_fails_naming_surface_and_secret_but_not_the_value(leak: str) -> None:
    surfaces = {"Terraform state of local": f'{{"data": "{leak}"}}', "ConfigMaps in olf-system": "clean"}

    with pytest.raises(E2EError) as raised:
        exposure.scan(CREDENTIALS, surfaces, link_surfaces={})

    message = str(raised.value)
    assert "Terraform state of local contains the value of Secret keycloak-client-perimeter/client-secret" in message
    assert "ConfigMaps" not in message
    assert CLIENT not in message and leak not in message


def test_values_too_short_to_scan_for_are_rejected_without_printing_them() -> None:
    with pytest.raises(E2EError, match=r"keycloak-admin-creds/password: shorter than 16") as raised:
        exposure.scan({"keycloak-admin-creds/password": "hunter2"}, {"logs": "hunter2"}, link_surfaces={})

    assert "hunter2" not in str(raised.value)


@pytest.mark.parametrize(
    "text",
    [
        "https://auth.olf.localhost/realms/r/login-actions/action-token?key=eyJhbGciOi",
        "GET /reset?token=0123456789abcdef",
        "jdbc:postgresql://db/kc?user=kc&password=s3cretpassw0rd",
    ],
)
def test_action_links_and_credential_query_strings_in_diagnostics_fail(text: str) -> None:
    with pytest.raises(E2EError, match="diagnostics bundle of olf-system contains a Keycloak action link"):
        exposure.scan({}, {}, link_surfaces={"diagnostics bundle of olf-system": f"line\n{text}\n"})


def test_ordinary_diagnostics_text_is_not_an_action_link() -> None:
    text = "KC_DB_PASSWORD: <set to the key 'password'>\npassword=<redacted>\naccess_token=abcdefghijklmnop\nok"

    assert exposure.find_action_links({"diagnostics bundle of olf-system": text}) == []


IDENTITY = {"clients": {"perimeter": {"secret_ref": {"name": "keycloak-client-perimeter", "key": "client-secret"}}}}
SMTP_REF = {"secretKeyRef": {"name": "mail", "key": "pw"}}
SMTP_JOB = {
    "items": [
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"env": [{"name": "SMTP_PASSWORD", "valueFrom": SMTP_REF}]}
                        ]
                    }
                }
            }
        }
    ]
}


def _cfg() -> Any:
    return SimpleNamespace(platform_namespace="olf-system", kube_context="ctx")


def test_credentials_come_from_the_secrets_the_contract_and_deployment_name(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        ("keycloak-admin-creds", "password"): ADMIN,
        ("keycloak-client-perimeter", "client-secret"): CLIENT,
        ("oauth2-proxy-cookie", "cookie-secret"): "k" * 32,
        ("mail", "pw"): "short",
    }

    def secret_value(name: str, key: str, _namespace: str, **_kwargs: Any) -> str:
        if (name, key) not in values:
            raise k8s.KubectlError("not found")
        return values[(name, key)]

    monkeypatch.setattr(exposure.k8s, "secret_value", secret_value)
    monkeypatch.setattr(exposure, "_kubectl_text", lambda _cfg, _args: json.dumps(SMTP_JOB))
    monkeypatch.setattr(exposure.log, "info", lambda _message: None)

    assert exposure._credentials(_cfg(), IDENTITY) == {
        "keycloak-admin-creds/password": ADMIN,
        "keycloak-client-perimeter/client-secret": CLIENT,
        "oauth2-proxy-cookie/cookie-secret": "k" * 32,
    }


def test_state_files_are_read_and_a_missing_one_is_unavailable(tmp_path: Path) -> None:
    platform, foundation = tmp_path / "local", tmp_path / "foundation"
    platform.mkdir()
    (platform / "terraform.tfstate").write_text('{"version": 4}')
    cfg = SimpleNamespace(contract_terraform_dir=platform, foundation_terraform_dir=foundation)

    assert exposure._state_surfaces(cfg) == {
        "Terraform state of local": '{"version": 4}',
        "Terraform state of foundation": None,
    }


def test_the_admin_api_path_that_sends_the_email_is_not_a_leak() -> None:
    exposure.scan({}, {}, link_surfaces={"diagnostics bundle": "PUT /admin/realms/r/users/1/execute-actions-email 204"})


def test_a_json_escaped_value_is_a_leak() -> None:
    password = 'long"pass\\word12345'
    text = json.dumps({"data": password})

    assert password not in text
    assert exposure.find_exposures({"mail/pw": password}, {"ConfigMaps in olf-system": text})


def test_an_external_issuer_still_scans_client_and_cookie_secrets_and_a_dotted_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read: list[tuple[str, str]] = []
    cookie = "k" * 32

    def secret_value(name: str, key: str, _namespace: str, **_kwargs: Any) -> str:
        read.append((name, key))
        if name == "oauth2-proxy-cookie":
            return cookie
        if name == "mail":
            return "m" * 20
        raise k8s.KubectlError("not found")

    job = json.loads(json.dumps(SMTP_JOB))
    env = job["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"][0]
    env["valueFrom"]["secretKeyRef"]["key"] = "smtp.pw"
    monkeypatch.setattr(exposure.k8s, "secret_value", secret_value)
    monkeypatch.setattr(exposure, "_kubectl_text", lambda _cfg, _args: json.dumps(job))
    monkeypatch.setattr(exposure.log, "info", lambda _message: None)

    found = exposure._credentials(_cfg(), IDENTITY, keycloak=False)

    assert found == {"oauth2-proxy-cookie/cookie-secret": cookie, "mail/smtp.pw": "m" * 20}
    assert ("keycloak-admin-creds", "password") not in read
    assert ("keycloak-client-perimeter", "client-secret") in read
    assert ("mail", "smtp\\.pw") in read


def test_failing_to_list_the_jobs_is_an_error_not_no_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "_kubectl_text", lambda _cfg, _args: None)

    with pytest.raises(E2EError, match="SMTP Secret"):
        exposure._smtp_password_refs(_cfg())
