"""Azure authentication through Azure Identity, and its Terraform bridge."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from olf.auth.state import AuthenticationError, _write_private_text, auth_home, load_state, save_state

_ARM_SCOPE = "https://management.azure.com/.default"
_AZURE_AUTOMATION_VARIABLES = {
    "AZURE_CLIENT_SECRET",
    "AZURE_CLIENT_CERTIFICATE_PATH",
    "AZURE_FEDERATED_TOKEN_FILE",
    "IDENTITY_ENDPOINT",
    "MSI_ENDPOINT",
}


_ARM_CLIENT_SECRET_VARS = ("ARM_CLIENT_ID", "ARM_TENANT_ID", "ARM_CLIENT_SECRET")
_ARM_CERTIFICATE_VARS = ("ARM_CLIENT_ID", "ARM_TENANT_ID", "ARM_CLIENT_CERTIFICATE_PATH")
_ARM_OIDC_TOKEN_VARS = ("ARM_CLIENT_ID", "ARM_TENANT_ID", "ARM_OIDC_TOKEN")
_ARM_OIDC_TOKEN_FILE_VARS = ("ARM_CLIENT_ID", "ARM_TENANT_ID", "ARM_OIDC_TOKEN_FILE_PATH")


def _uses_azure_automation(environ: Mapping[str, str]) -> bool:
    if _azure_managed_identity_client_id(environ) is not None:
        return True
    if any(environ.get(name) for name in _AZURE_AUTOMATION_VARIABLES):
        return True
    # AzureRM's provider reads these directly - it never goes through
    # azure-identity - so Terraform authenticates correctly with only ARM_*
    # set while `DefaultAzureCredential`/`EnvironmentCredential`, which only
    # recognize the differently-named AZURE_* forms, see no automation
    # source at all.
    return any(
        all(environ.get(name) for name in var_group)
        for var_group in (
            _ARM_CLIENT_SECRET_VARS,
            _ARM_CERTIFICATE_VARS,
            _ARM_OIDC_TOKEN_VARS,
            _ARM_OIDC_TOKEN_FILE_VARS,
        )
    )


def _azure_managed_identity_client_id(environ: Mapping[str, str]) -> str | None:
    """Return an explicitly selected user-assigned managed identity, if any."""
    azure_client_id = environ.get("AZURE_CLIENT_ID")
    if azure_client_id:
        return azure_client_id
    use_msi = environ.get("ARM_USE_MSI", "").strip().lower()
    if use_msi in {"1", "true", "yes"}:
        return environ.get("ARM_CLIENT_ID") or None
    return None


def _azure_automation_credential(environ: Mapping[str, str]) -> Any | None:
    """Translate AzureRM's native ARM_* automation variables into a credential.

    Returns `None` when none of the ARM_* forms are present, so the caller
    falls back to `DefaultAzureCredential` for the AZURE_*/managed-identity
    forms `_uses_azure_automation` also recognizes.
    """
    managed_identity_client_id = _azure_managed_identity_client_id(environ)
    if managed_identity_client_id is not None:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=managed_identity_client_id)

    client_id = environ.get("ARM_CLIENT_ID")
    tenant_id = environ.get("ARM_TENANT_ID")
    if not client_id or not tenant_id:
        return None
    client_secret = environ.get("ARM_CLIENT_SECRET")
    if client_secret:
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    certificate_path = environ.get("ARM_CLIENT_CERTIFICATE_PATH")
    if certificate_path:
        from azure.identity import CertificateCredential

        return CertificateCredential(tenant_id=tenant_id, client_id=client_id, certificate_path=certificate_path)
    oidc_token = environ.get("ARM_OIDC_TOKEN")
    oidc_token_file = environ.get("ARM_OIDC_TOKEN_FILE_PATH")
    if oidc_token or oidc_token_file:
        from azure.identity import ClientAssertionCredential

        def _assertion() -> str:
            if oidc_token:
                return oidc_token
            return Path(str(oidc_token_file)).read_text(encoding="utf-8").strip()

        return ClientAssertionCredential(tenant_id=tenant_id, client_id=client_id, func=_assertion)
    return None


def _azure_managed_identity_available() -> bool:
    """Detect a system-assigned Azure managed identity the environment cannot show.

    App Service/Functions managed identity sets IDENTITY_ENDPOINT/MSI_ENDPOINT,
    already covered by `_uses_azure_automation`. A VM/VMSS system-assigned
    identity is discovered through IMDS and sets nothing - same rationale and
    latency bound as `_aws_instance_profile_available`; a token fetched here
    is simply discarded, the real one is minted fresh when actually used.
    """
    from azure.identity import ManagedIdentityCredential

    try:
        ManagedIdentityCredential(connection_timeout=1, retry_total=0).get_token(_ARM_SCOPE)
        return True
    except Exception:
        return False


def login_azure(
    *,
    tenant_id: str | None = None,
    subscription_id: str | None = None,
    device_code: bool = False,
    environ: Mapping[str, str] | None = None,
    choose: Any | None = None,
) -> dict[str, Any]:
    """Authenticate with Azure Identity; it owns the Microsoft browser UI."""
    from azure.identity import DeviceCodeCredential, InteractiveBrowserCredential
    from azure.mgmt.resource.subscriptions import SubscriptionClient

    env = environ or os.environ
    options = _azure_cache_persistence_options()
    credential: DeviceCodeCredential | InteractiveBrowserCredential
    if device_code:
        credential = DeviceCodeCredential(tenant_id=tenant_id, cache_persistence_options=options)
    else:
        credential = InteractiveBrowserCredential(tenant_id=tenant_id, cache_persistence_options=options)
    record = credential.authenticate(scopes=[_ARM_SCOPE])
    subscriptions = list(SubscriptionClient(credential).subscriptions.list())
    selected_item = _resolve_subscription(subscription_id, subscriptions, choose)
    state = {
        "source": "olf-browser",
        "tenant_id": selected_item.tenant_id or tenant_id or "",
        "subscription_id": str(selected_item.subscription_id),
        "principal": getattr(record, "username", ""),
        "authentication_record": record.serialize(),
    }
    save_state("azure", state, env)
    return state


def _azure_cache_persistence_options() -> Any:
    """Return the shared Azure SDK cache policy for browser and device login.

    Azure Identity otherwise rejects persistent caches on headless Linux hosts
    without a system keyring before the device-code flow can begin. The SDK
    owns this cache; OLF persists only its authentication record and selection.
    """
    from azure.identity import TokenCachePersistenceOptions

    return TokenCachePersistenceOptions(name="openlakeforge-auth", allow_unencrypted_storage=True)


def _choose_subscription(subscriptions: list[Any], choose: Any | None) -> str:
    values = [str(item.subscription_id) for item in subscriptions if item.subscription_id]
    if len(values) == 1:
        return values[0]
    if not values:
        raise AuthenticationError("No Azure subscriptions are available for this identity.")
    if choose is None:
        raise AuthenticationError("Multiple Azure subscriptions are available; pass --subscription-id.")
    return str(choose(values, "Azure subscription"))


def _resolve_subscription(explicit: str | None, subscriptions: list[Any], choose: Any | None) -> Any:
    """Return the caller's explicit subscription, or prompt for one.

    An explicit `--subscription-id` is validated against what the identity
    actually has access to. Persisting an unlisted value would report
    "authentication ready" and then raise an unhandled `StopIteration`
    (`next()` over the now-empty match) the CLI's `except AuthenticationError`
    handler cannot catch, instead of an actionable message.
    """
    if not explicit:
        selected = _choose_subscription(subscriptions, choose)
        return next(item for item in subscriptions if str(item.subscription_id) == selected)
    match = next((item for item in subscriptions if str(item.subscription_id) == explicit), None)
    if match is None:
        available = ", ".join(sorted(str(item.subscription_id) for item in subscriptions)) or "(none)"
        raise AuthenticationError(f"Azure subscription {explicit!r} is not available; offered: {available}.")
    return match


def azure_credential(environ: Mapping[str, str]) -> Any:
    """Resolve the selected Azure credential without opening a browser."""
    from azure.identity import (
        AuthenticationRecord,
        AzureCliCredential,
        DefaultAzureCredential,
        InteractiveBrowserCredential,
    )

    if _uses_azure_automation(environ):
        arm_credential = _azure_automation_credential(environ)
        if arm_credential is not None:
            return arm_credential
        return DefaultAzureCredential(
            exclude_azure_cli_credential=True,
            exclude_interactive_browser_credential=True,
        )
    state = load_state("azure", environ)
    if state is None:
        return DefaultAzureCredential(exclude_interactive_browser_credential=True)
    if _azure_managed_identity_available():
        return DefaultAzureCredential(
            exclude_azure_cli_credential=True,
            exclude_interactive_browser_credential=True,
        )
    source = state.get("source")
    if source == "azure-cli":
        return AzureCliCredential(tenant_id=state.get("tenant_id") or "")
    if source == "olf-browser":
        record = AuthenticationRecord.deserialize(str(state["authentication_record"]))
        return InteractiveBrowserCredential(
            tenant_id=state.get("tenant_id") or None,
            authentication_record=record,
            cache_persistence_options=_azure_cache_persistence_options(),
            disable_automatic_authentication=True,
        )
    raise AuthenticationError("unknown Azure authentication source; run 'olf auth login --provider azure'.")


def selected_azure_subscription(environ: Mapping[str, str]) -> str | None:
    """Return the subscription saved by `olf auth login`, if any.

    `doctor()` builds its preflight environment from
    `credential_selection_environment` alone (no `terraform_auth_environment`
    overlay), so `ARM_SUBSCRIPTION_ID` is not guaranteed to be set even though
    a saved OLF session exists. Adapters consult this as their last fallback
    so authentication succeeds identically whether or not Terraform's overlay
    ran first.
    """
    state = load_state("azure", environ)
    if state is None:
        return None
    subscription_id = state.get("subscription_id")
    return str(subscription_id) if subscription_id else None


def terraform_auth_environment(environ: Mapping[str, str]) -> dict[str, str]:
    if _uses_azure_automation(environ):
        return {}
    state = load_state("azure", environ)
    if state is None:
        return {}
    if _azure_managed_identity_available():
        return {}
    if state.get("source") == "azure-cli":
        return {
            "ARM_SUBSCRIPTION_ID": str(state["subscription_id"]),
            "ARM_TENANT_ID": str(state.get("tenant_id", "")),
        }
    if state.get("source") == "olf-browser":
        bridge_dir = auth_home(environ) / "azure-terraform-bridge"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        bridge = bridge_dir / "az"
        _write_private_text(bridge, f"#!{sys.executable}\nfrom olf.azure_bridge import main\nmain()\n")
        os.chmod(bridge, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return {
            "PATH": f"{bridge_dir}{os.pathsep}{environ.get('PATH', os.environ.get('PATH', ''))}",
            "ARM_SUBSCRIPTION_ID": str(state["subscription_id"]),
            "ARM_TENANT_ID": str(state.get("tenant_id", "")),
        }
    return {}


def adopt_azure_cli(
    *,
    tenant_id: str | None = None,
    subscription_id: str | None = None,
    environ: Mapping[str, str] | None = None,
    choose: Any | None = None,
) -> dict[str, Any]:
    from azure.identity import AzureCliCredential
    from azure.mgmt.resource.subscriptions import SubscriptionClient

    credential = AzureCliCredential(tenant_id=tenant_id or "")
    subscriptions = list(SubscriptionClient(credential).subscriptions.list())
    item = _resolve_subscription(subscription_id, subscriptions, choose)
    state = {
        "source": "azure-cli",
        "tenant_id": item.tenant_id or tenant_id or "",
        "subscription_id": str(item.subscription_id),
    }
    save_state("azure", state, environ)
    return state
