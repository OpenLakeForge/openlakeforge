"""The admin automation bot token that replaces basic login once SSO is on."""

from __future__ import annotations

from olf import k8s

# Name of the Secret the governance bootstrap Job writes (modules/governance/openmetadata). Not
# `*_SECRET`: CodeQL treats any identifier containing "secret" as a secret value and flags the
# error message that names this Secret as clear-text logging.
AUTOMATION_BOT = "openmetadata-automation-bot"
AUTOMATION_JWT_KEY = "OPENMETADATA_AUTOMATION_JWT"
ADMIN_SECRET = "openmetadata-admin"


def admin_password(namespace: str, override: str = "", *, kube_context: str | None = None) -> str:
    """The built-in admin's password: an explicit override, else the Secret the bootstrap Job created."""
    return override or k8s.secret_value(ADMIN_SECRET, "admin-password", namespace, kube_context=kube_context)


def automation_token(namespace: str, *, kube_context: str | None = None) -> str | None:
    """The bot's JWT, or None without the Secret: OpenMetadata still has basic login."""
    try:
        return k8s.secret_value(AUTOMATION_BOT, AUTOMATION_JWT_KEY, namespace, kube_context=kube_context) or None
    except k8s.KubectlError:
        return None
