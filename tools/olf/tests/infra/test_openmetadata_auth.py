from __future__ import annotations

import pytest
from openlakeforge_services.openmetadata import OpenMetadataClient

from olf.infra import k8s
from olf.infra.openmetadata import _auth
from olf.infra.openmetadata._reconciliation import OpenMetadataReconciler


def test_automation_token_is_the_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, str]] = []

    def secret_value(name: str, key: str, namespace: str, *, kube_context: str | None = None) -> str:
        seen.append((name, key, namespace))
        return "jwt"

    monkeypatch.setattr(k8s, "secret_value", secret_value)

    assert _auth.automation_token("olf-system") == "jwt"
    assert seen == [("openmetadata-automation-bot", "OPENMETADATA_AUTOMATION_JWT", "olf-system")]


def test_automation_token_is_none_without_sso(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: object, **_kwargs: object) -> str:
        raise k8s.KubectlError("secret not found")

    monkeypatch.setattr(k8s, "secret_value", missing)

    assert _auth.automation_token("olf-system") is None


def test_basic_login_is_skipped_with_the_automation_token() -> None:
    client = OpenMetadataClient("http://om", token="bot")
    # The config is read only by the basic login, which must not be reached.
    reconciler = OpenMetadataReconciler(None, client)  # type: ignore[arg-type]

    reconciler.login()

    assert client.token == "bot"


def test_admin_password_is_the_secret_unless_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "secret_value", lambda name, key, namespace, **_kw: f"{name}/{key}")

    assert _auth.admin_password("olf-system") == "openmetadata-admin/admin-password"
    assert _auth.admin_password("olf-system", "chosen") == "chosen"
