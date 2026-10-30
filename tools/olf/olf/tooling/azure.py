"""Azure SDK adapter used by cloud deployment code, with no ``az`` dependency."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from olf.tooling.process import CommandResult

_ACR_USERNAME = "00000000-0000-0000-0000-000000000000"


class AzureSdk:
    """Small, injectable Azure SDK facade matching the former CLI adapter."""

    def __init__(
        self,
        credential: Any | None = None,
        subscription_id: str | None = None,
        *,
        subscription_client_factory: Any | None = None,
        aks_client_factory: Any | None = None,
        post: Any = requests.post,
        **_: Any,
    ) -> None:
        # See AwsSdk: old callers may still pass a ProcessRunner first.
        self._credential_override = credential if credential is None or hasattr(credential, "get_token") else None
        self._subscription_id = subscription_id
        self._tenant_id: str | None = None
        self._subscription_client_factory = subscription_client_factory
        self._aks_client_factory = aks_client_factory
        self._post = post

    def _credential(self, env: Mapping[str, str] | None = None) -> Any:
        if self._credential_override is not None:
            return self._credential_override
        from olf.auth import azure_credential

        return azure_credential(env or os.environ)

    def _subscription(self, env: Mapping[str, str] | None = None) -> str:
        selected = self._subscription_id or (env or {}).get("ARM_SUBSCRIPTION_ID") or self._saved_subscription(env)
        if selected:
            return selected
        account = self.account_show(env=env)
        return str(account["id"])

    def _saved_subscription(self, env: Mapping[str, str] | None) -> str | None:
        """Fall back to the subscription `olf auth login` saved.

        `doctor()` builds its preflight environment without Terraform's
        `ARM_SUBSCRIPTION_ID` overlay, so a caller with only saved OLF state
        and no explicit selection still needs to resolve the right
        subscription among several.
        """
        from olf.auth import selected_azure_subscription

        return selected_azure_subscription(env or os.environ)

    def account_show(self, *, env: Mapping[str, str] | None = None) -> Any:
        from azure.mgmt.resource.subscriptions import SubscriptionClient

        factory = self._subscription_client_factory or SubscriptionClient
        subscriptions = list(factory(self._credential(env)).subscriptions.list())
        desired = self._subscription_id or (env or {}).get("ARM_SUBSCRIPTION_ID") or self._saved_subscription(env)
        selected = next((item for item in subscriptions if item.subscription_id == desired), None)
        if selected is None:
            if len(subscriptions) != 1:
                raise RuntimeError("Azure subscription is not selected; run 'olf auth login --provider azure'.")
            selected = subscriptions[0]
        self._subscription_id = str(selected.subscription_id)
        self._tenant_id = str(selected.tenant_id) if selected.tenant_id else None
        return {
            "id": selected.subscription_id,
            "name": selected.display_name,
            "tenantId": selected.tenant_id,
            "state": str(selected.state) if selected.state else "Enabled",
        }

    def account_set(self, subscription: str, *, env: Mapping[str, str] | None = None) -> CommandResult:  # noqa: ARG002
        self._subscription_id = subscription
        return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)

    def _aks_client(self, env: Mapping[str, str] | None = None) -> Any:
        from azure.mgmt.containerservice import ContainerServiceClient

        factory = self._aks_client_factory or ContainerServiceClient
        return factory(self._credential(env), self._subscription(env))

    def aks_get_credentials(
        self,
        cluster_name: str,
        *,
        resource_group: str,
        kubeconfig_path: Path,
        overwrite: bool = True,  # noqa: ARG002 - SDK returns the complete kubeconfig.
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        result = self._aks_client(env).managed_clusters.list_cluster_user_credentials(resource_group, cluster_name)
        value = result.kubeconfigs[0].value
        contents = value.decode() if isinstance(value, bytes) else str(value)
        _write_private(kubeconfig_path, contents)
        return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)

    def acr_login(self, registry_name: str, *, env: Mapping[str, str] | None = None) -> str:
        credential = self._credential(env)
        access_token = credential.get_token("https://management.azure.com/.default").token
        host = registry_name if "." in registry_name else f"{registry_name}.azurecr.io"
        tenant_id = (env or {}).get("ARM_TENANT_ID") or self._tenant_id or self.account_show(env=env)["tenantId"]
        response = self._post(
            f"https://{host}/oauth2/exchange",
            data={
                "grant_type": "access_token",
                "service": host,
                "access_token": access_token,
                "tenant": tenant_id,
            },
            timeout=30,
        )
        response.raise_for_status()
        return str(response.json()["refresh_token"])

    def aks_show(
        self,
        cluster_name: str,
        *,
        resource_group: str,
        env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> CommandResult:
        try:
            self._aks_client(env).managed_clusters.get(resource_group, cluster_name)
        except Exception as exc:
            if check:
                raise
            return CommandResult(argv=(), returncode=1, stdout="", stderr=str(exc), duration_seconds=0.0)
        return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)


def _write_private(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(raw_path, stat.S_IRUSR | stat.S_IWUSR)
        Path(raw_path).replace(path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        Path(raw_path).unlink(missing_ok=True)
        raise


AzureCli = AzureSdk
ACR_USERNAME = _ACR_USERNAME
