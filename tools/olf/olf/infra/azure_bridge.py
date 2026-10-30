"""Minimal Azure CLI protocol adapter used privately by AzureRM Terraform."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

from azure.mgmt.resource.subscriptions import SubscriptionClient

from olf.auth import AuthenticationError, azure_credential, load_state

_SUPPORTED_ACCOUNT_COMMANDS = (["account", "list"], ["account", "show"], ["account", "get-access-token"])


def _args(argv: list[str]) -> list[str]:
    return [value for value in argv if value not in {"-o=json", "--output=json", "-o", "json", "--output"}]


def _subscriptions() -> tuple[Any, list[Any]]:
    state = load_state("azure")
    if not state or state.get("source") != "olf-browser":
        raise AuthenticationError("OLF-managed Azure browser authentication is required.")
    credential = azure_credential(os.environ)
    return state, list(SubscriptionClient(credential).subscriptions.list())


def _account(subscription: Any) -> dict[str, Any]:
    return {
        "environmentName": "AzureCloud",
        "id": subscription.subscription_id,
        "isDefault": True,
        "name": subscription.display_name,
        "state": str(subscription.state) if subscription.state else "Enabled",
        "tenantId": subscription.tenant_id,
        "user": {"name": "olf-sdk", "type": "user"},
    }


def main() -> None:
    try:
        args = _args(sys.argv[1:])
        if args == ["version"]:
            _emit({"azure-cli": "2.61.0", "azure-cli-core": "2.61.0", "extensions": {}})
            return
        # Identify the command by its leading verb pair only: AzureRM appends
        # option flags (--scope, --tenant, --subscription, --all) that are not
        # part of the command identity. Reject anything outside the contract
        # here, before resolving credentials, so an unsupported command says
        # so plainly instead of failing later inside a credential call.
        command = args[:2]
        if command not in _SUPPORTED_ACCOUNT_COMMANDS:
            raise AuthenticationError("Terraform requested an unsupported Azure CLI command.")
        state, subscriptions = _subscriptions()
        selected = str(state["subscription_id"])
        if command == ["account", "list"]:
            _emit([_account(item) | {"isDefault": item.subscription_id == selected} for item in subscriptions])
            return
        if command == ["account", "show"]:
            item = next(item for item in subscriptions if item.subscription_id == selected)
            _emit(_account(item))
            return
        scope = _option(args, "--scope") or "https://management.azure.com/.default"
        tenant = _option(args, "--tenant") or state.get("tenant_id", "")
        token = azure_credential(os.environ).get_token(scope)
        _emit(
            {
                "accessToken": token.token,
                "expiresOn": datetime.fromtimestamp(token.expires_on, UTC)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S.%f"),
                "tenant": tenant,
                "tokenType": "Bearer",
            }
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def _option(args: list[str], name: str) -> str | None:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _emit(value: Any) -> None:
    print(json.dumps(value))  # noqa: T201 - AzureRM consumes this protocol on stdout.


if __name__ == "__main__":
    main()
