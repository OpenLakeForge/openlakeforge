"""The read-only baseline SSO self-signup relies on, for the bootstrap (#562).

Ports the SSO block at the end of the governance module's `bootstrap.sh.tftpl`. Self-signup
makes every admitted person a user, and every user inherits the Organization default role
(DataConsumer, which edits descriptions, tags and tiers) and may take ownership of unowned
assets. Read-only is the Organization policy without either (identity-service-permissions.md).
The switch to SSO itself is `deploy-metadata`'s: the issuer only resolves once Traefik exists (#432).
"""

from __future__ import annotations

from typing import Any

from openlakeforge_services.openmetadata import OpenMetadataClient

NO_OWNER_RULE = "OrganizationPolicy-NoOwner-Rule"


def _remove(client: OpenMetadataClient, path: str, field: str, indexes: list[int]) -> None:
    """JSON-patch the entries out, highest index first so each removal leaves the next index valid."""
    if not indexes:
        return
    ops: list[Any] = [{"op": "remove", "path": f"/{field}/{i}"} for i in sorted(indexes, reverse=True)]
    client.request("PATCH", path, json_body=ops, content_type="application/json-patch+json")


def restrict_signup_defaults(client: OpenMetadataClient) -> None:
    """Drop the Organization team's default roles and the policy's no-owner rule; a no-op once done."""
    org = client.request("GET", "/api/v1/teams/name/Organization", params={"fields": "defaultRoles"})
    _remove(client, "/api/v1/teams/name/Organization", "defaultRoles", list(range(len(org.get("defaultRoles") or []))))
    policy = client.request("GET", "/api/v1/policies/name/OrganizationPolicy")
    rules = policy.get("rules") or []
    _remove(
        client,
        "/api/v1/policies/name/OrganizationPolicy",
        "rules",
        [i for i, rule in enumerate(rules) if rule.get("name") == NO_OWNER_RULE],
    )
