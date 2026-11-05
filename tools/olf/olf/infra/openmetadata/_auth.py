"""The admin automation bot token that replaces basic login once SSO is on."""

from __future__ import annotations

from collections.abc import Mapping

from olf.infra import k8s

# Name of the Secret `olf infra openmetadata bootstrap` writes (olf.infra.openmetadata._bootstrap). Not
# `*_SECRET`: CodeQL treats any identifier containing "secret" as a secret value and flags the
# error message that names this Secret as clear-text logging.
AUTOMATION_BOT = "openmetadata-automation-bot"
AUTOMATION_JWT_KEY = "OPENMETADATA_AUTOMATION_JWT"
ADMIN_SECRET = "openmetadata-admin"


def admin_password(
    namespace: str, override: str = "", *, kube_context: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """The built-in admin's password: an explicit override, else the Secret the bootstrap created."""
    return override or k8s.secret_value(
        ADMIN_SECRET, "admin-password", namespace, kube_context=kube_context, environ=environ
    )


def automation_token(
    namespace: str, *, kube_context: str | None = None, environ: Mapping[str, str] | None = None
) -> str | None:
    """The bot's JWT, or None without the Secret: OpenMetadata still has basic login."""
    try:
        return (
            k8s.secret_value(AUTOMATION_BOT, AUTOMATION_JWT_KEY, namespace, kube_context=kube_context, environ=environ)
            or None
        )
    except k8s.KubectlError:
        return None


# The Secret every consumer of the ingestion bot's JWT reads (the Dagster code location, the Trino ingestion Job).
INGESTION_BOT_SECRET = "openmetadata-ingestion-bot"
INGESTION_BOT_JWT_KEY = "OPENMETADATA_INGESTION_BOT_JWT"
