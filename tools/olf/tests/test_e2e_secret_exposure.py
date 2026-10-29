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
CREDENTIALS = {"dex-admin-creds/password": ADMIN, "dex-client-perimeter/client-secret": CLIENT}


def test_clean_surfaces_pass_and_an_unreadable_one_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(exposure.log, "info", messages.append)

    exposure.scan(CREDENTIALS, {"ConfigMaps in olf-system": "{}", "logs of pod/x": None})

    assert messages == ["Skipping the exposure scan of logs of pod/x: unavailable."]


@pytest.mark.parametrize("leak", [CLIENT, base64.b64encode(CLIENT.encode()).decode()])
def test_a_leaking_surface_fails_naming_surface_and_secret_but_not_the_value(leak: str) -> None:
    surfaces = {"Terraform state of local": f'{{"data": "{leak}"}}', "ConfigMaps in olf-system": "clean"}

    with pytest.raises(E2EError) as raised:
        exposure.scan(CREDENTIALS, surfaces)

    message = str(raised.value)
    assert "Terraform state of local contains the value of Secret dex-client-perimeter/client-secret" in message
    assert "ConfigMaps" not in message
    assert CLIENT not in message and leak not in message


def test_values_too_short_to_scan_for_are_rejected_without_printing_them() -> None:
    with pytest.raises(E2EError, match=r"dex-admin-creds/password: shorter than 16") as raised:
        exposure.scan({"dex-admin-creds/password": "hunter2"}, {"logs": "hunter2"})

    assert "hunter2" not in str(raised.value)


IDENTITY = {"clients": {"perimeter": {"secret_ref": {"name": "dex-client-perimeter", "key": "client-secret"}}}}


def _kubectl_stub(_cfg: Any, _args: list[str]) -> str:
    """The Secret name listing: one Trino principal."""
    return "secret/trino-credential-olf-dev-runtime"


def _cfg() -> Any:
    return SimpleNamespace(platform_namespace="olf-system", kube_context="ctx")


def test_credentials_come_from_the_secrets_the_contract_and_deployment_name(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        ("dex-client-perimeter", "client-secret"): CLIENT,
        ("oauth2-proxy-cookie", "cookie-secret"): "k" * 32,
        ("trino-credential-olf-dev-runtime", "password"): "d" * 32,
        ("trino-internal-secret", "internal-communication\\.shared-secret"): "i" * 32,
    }

    def secret_value(name: str, key: str, _namespace: str, **_kwargs: Any) -> str:
        if (name, key) not in values:
            raise k8s.KubectlError("not found")
        return values[(name, key)]

    monkeypatch.setattr(exposure.k8s, "secret_value", secret_value)
    monkeypatch.setattr(exposure, "_kubectl_text", _kubectl_stub)
    monkeypatch.setattr(exposure.log, "info", lambda _message: None)

    assert exposure._credentials(_cfg(), IDENTITY) == {
        "dex-client-perimeter/client-secret": CLIENT,
        "oauth2-proxy-cookie/cookie-secret": "k" * 32,
        "trino-credential-olf-dev-runtime/password": "d" * 32,
        "trino-internal-secret/internal-communication.shared-secret": "i" * 32,
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


def test_a_json_escaped_value_is_a_leak() -> None:
    password = 'long"pass\\word12345'
    text = json.dumps({"data": password})

    assert password not in text
    assert exposure.find_exposures({"mail/pw": password}, {"ConfigMaps in olf-system": text})


def test_every_stage_namespace_is_scanned_not_only_the_selected_one() -> None:
    cfg = SimpleNamespace(platform_namespace="olf-system", namespace="olf-dev", shared_namespace="olf-system")
    contracts = {"stages": {"dev": {"namespace": "olf-dev"}, "prod": {"namespace": "olf-prod"}}}

    assert exposure.scan_namespaces(cfg, contracts) == ("olf-system", "olf-dev", "olf-prod")
    # Single-namespace roots carry no stage namespaces; the health namespaces are the whole set.
    assert exposure.scan_namespaces(cfg, {"stages": {"dev": {}}}) == ("olf-system", "olf-dev")


def test_failing_to_list_the_secrets_is_an_error_not_no_trino_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "_kubectl_text", lambda _cfg, _args: None)

    with pytest.raises(E2EError, match="cannot list the Secrets"):
        exposure._trino_secret_refs(_cfg())


def test_migrated_credentials_are_found_in_every_namespace_by_name_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        ("olf-system", "openmetadata-admin", "admin-password"): "o" * 28,
        ("olf-system", "postgresql-admin-creds", "postgres-password"): "p" * 32,
        ("olf-dev", "superset-credentials", "secret-key"): "s" * 64,
        ("olf-dev", "superset-credentials", "admin-password"): "short",
        ("olf-dev", "postgresql-dagster-dev-creds", "postgresql-password"): "d" * 32,
    }

    def secret_value(name: str, key: str, namespace: str, **_kwargs: Any) -> str:
        if (namespace, name, key) not in values:
            raise k8s.KubectlError("not found")
        return values[(namespace, name, key)]

    def listing(_cfg: Any, args: list[str]) -> str:
        names = {"olf-system": ["postgresql-admin-creds"], "olf-dev": ["postgresql-dagster-dev-creds", "dagster-x"]}
        return "\n".join(f"secret/{name}" for name in names[args[3]])

    monkeypatch.setattr(exposure.k8s, "secret_value", secret_value)
    monkeypatch.setattr(exposure, "_kubectl_text", listing)

    assert exposure.migrated_credentials(_cfg(), ("olf-system", "olf-dev")) == {
        "olf-system/openmetadata-admin/admin-password": "o" * 28,
        "olf-system/postgresql-admin-creds/postgres-password": "p" * 32,
        "olf-dev/superset-credentials/secret-key": "s" * 64,
        "olf-dev/postgresql-dagster-dev-creds/postgresql-password": "d" * 32,
    }
