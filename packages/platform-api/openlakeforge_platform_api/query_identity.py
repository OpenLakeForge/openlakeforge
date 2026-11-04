"""The person's Trino identity, built from the bearer token the API already validated (ADR 0019, ADR 0021).

Nothing is stored: the token is the one on this request, and Trino and Polaris
authorize the person from it. The shape matches `olf query`
(tools/olf/olf/platform/query.py): `X-Trino-User` is the e-mail, and the same
token is the bearer and the Polaris extra credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from openlakeforge_platform_api.tokens import Principal


@dataclass(frozen=True)
class TrinoIdentity:
    user: str
    # Excluded from repr so a log line or traceback of the identity cannot carry it.
    token: str = field(repr=False)

    @property
    def extra_credential(self) -> str:
        """The value of `X-Trino-Extra-Credential`, read by Polaris at submission."""
        return f"token={self.token}"


def trino_identity(request: Request) -> TrinoIdentity:
    """A route's dependency. Refuses with 404 where query is off, and with 403 when the token lacks what Trino needs."""
    if not request.app.state.settings.person_query:
        raise HTTPException(status_code=404, detail="per-person query is not available on this deployment")
    principal: Principal | None = getattr(request.state, "principal", None)
    token: str | None = getattr(request.state, "token", None)
    if principal is None or token is None or principal.role is None:
        raise HTTPException(status_code=403, detail="no OpenLakeForge role assigned; ask a platform-admin")
    if not principal.email:
        raise HTTPException(
            status_code=403, detail="the token carries no e-mail, which Trino names you by; sign in again"
        )
    if principal.federated_claims is None:
        raise HTTPException(status_code=403, detail="the token lacks the federated claims Polaris reads; sign in again")
    return TrinoIdentity(user=principal.email, token=token)
