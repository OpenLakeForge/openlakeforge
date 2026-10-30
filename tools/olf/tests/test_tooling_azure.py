from __future__ import annotations

import stat
from pathlib import Path

from olf.tooling.azure import AzureSdk


class _Credential:
    def get_token(self, *_args):  # noqa: ANN001, ANN202
        return type("Token", (), {"token": "arm-token"})()


class _Subscription:
    subscription_id = "sub-id"
    display_name = "OpenLakeForge"
    tenant_id = "tenant-id"
    state = "Enabled"


class _OtherSubscription:
    subscription_id = "other-sub-id"
    display_name = "Other"
    tenant_id = "other-tenant-id"
    state = "Enabled"


def _subscriptions(_credential):  # noqa: ANN001, ANN202
    return type(
        "Client", (), {"subscriptions": type("Subscriptions", (), {"list": lambda *_args: [_Subscription()]})()}
    )()


def _multiple_subscriptions(_credential):  # noqa: ANN001, ANN202
    return type(
        "Client",
        (),
        {
            "subscriptions": type(
                "Subscriptions", (), {"list": lambda *_args: [_Subscription(), _OtherSubscription()]}
            )()
        },
    )()


def test_account_show_uses_subscription_sdk() -> None:
    account = AzureSdk(_Credential(), subscription_client_factory=_subscriptions).account_show()

    assert account == {"id": "sub-id", "name": "OpenLakeForge", "tenantId": "tenant-id", "state": "Enabled"}


def test_account_set_keeps_selection_in_process() -> None:
    azure = AzureSdk(_Credential(), subscription_client_factory=_subscriptions)

    assert azure.account_set("sub-id").ok


def test_aks_get_credentials_writes_returned_kubeconfig(tmp_path: Path) -> None:
    result = type("Result", (), {"kubeconfigs": [type("Config", (), {"value": b"apiVersion: v1\n"})()]})()
    aks = type(
        "Aks",
        (),
        {"managed_clusters": type("Clusters", (), {"list_cluster_user_credentials": lambda *_args: result})()},
    )()
    azure = AzureSdk(_Credential(), subscription_id="sub-id", aks_client_factory=lambda *_args: aks)
    path = tmp_path / "azure.yaml"

    assert azure.aks_get_credentials("cluster", resource_group="group", kubeconfig_path=path).ok
    assert path.read_text() == "apiVersion: v1\n"
    assert path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_acr_login_exchanges_arm_token_for_docker_refresh_token() -> None:
    response = type(
        "Response", (), {"raise_for_status": lambda *_args: None, "json": lambda *_args: {"refresh_token": "acr-token"}}
    )()
    calls = []
    azure = AzureSdk(
        _Credential(),
        subscription_client_factory=_subscriptions,
        post=lambda *args, **kwargs: calls.append((args, kwargs)) or response,
    )

    assert azure.acr_login("openlakeforgeacr") == "acr-token"
    assert calls[0][0][0] == "https://openlakeforgeacr.azurecr.io/oauth2/exchange"
    assert calls[0][1]["data"]["tenant"] == "tenant-id"


def test_aks_show_maps_sdk_failures_to_not_ok_result() -> None:
    clusters = type("Clusters", (), {"get": lambda *_args: (_ for _ in ()).throw(RuntimeError("not found"))})()
    aks = type("Aks", (), {"managed_clusters": clusters})()

    result = AzureSdk(_Credential(), subscription_id="sub-id", aks_client_factory=lambda *_args: aks).aks_show(
        "cluster", resource_group="group"
    )

    assert not result.ok
    assert "not found" in result.stderr


def test_account_show_falls_back_to_the_saved_olf_login_subscription(monkeypatch) -> None:  # noqa: ANN001
    """`doctor()` builds its preflight environment without Terraform's
    `ARM_SUBSCRIPTION_ID` overlay (see `olf.deployment.cloud.provider.doctor`),
    so an identity with several subscriptions and only saved OLF state must
    still resolve to the one the user selected during `olf auth login`
    instead of raising "subscription is not selected".
    """
    import olf.auth as auth

    monkeypatch.setattr(auth, "selected_azure_subscription", lambda _environ: "other-sub-id")

    account = AzureSdk(_Credential(), subscription_client_factory=_multiple_subscriptions).account_show()

    assert account["id"] == "other-sub-id"
