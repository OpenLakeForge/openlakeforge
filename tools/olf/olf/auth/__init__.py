"""Provider authentication state and SDK credential resolution.

This package intentionally contains no HTTP UI.  AWS returns its own device
authorization URL and Azure Identity opens Microsoft Entra's browser flow.
"""

from __future__ import annotations

from collections.abc import Mapping

from olf.auth import aws, azure
from olf.auth.aws import aws_process_credentials, aws_session, login_aws
from olf.auth.azure import adopt_azure_cli, azure_credential, login_azure, selected_azure_subscription
from olf.auth.state import AuthenticationError, auth_home, clear_state, load_state, save_state

__all__ = [
    "AuthenticationError",
    "adopt_azure_cli",
    "auth_home",
    "aws_process_credentials",
    "aws_session",
    "azure_credential",
    "clear_state",
    "credential_selection_environment",
    "load_state",
    "login_aws",
    "login_azure",
    "save_state",
    "selected_azure_subscription",
    "terraform_auth_environment",
]


def credential_selection_environment(provider: str, environ: Mapping[str, str]) -> dict[str, str]:
    """Return only cloud credential-selection variables for a child command.

    Deployment commands inherit the process environment, but SDK adapters use
    their explicit environment mapping to choose a credential source. Keeping
    this narrow avoids putting unrelated user environment values in diagnostic
    output while preserving automation precedence.
    """
    prefixes = ("AWS_",) if provider == "aws" else ("ARM_", "AZURE_", "IDENTITY_", "MSI_")
    selected = {name: value for name, value in environ.items() if name.startswith(prefixes)}
    if environ.get("OLF_HOME"):
        selected["OLF_HOME"] = environ["OLF_HOME"]
    return selected


def terraform_auth_environment(provider: str, environ: Mapping[str, str]) -> dict[str, str]:
    """Return Terraform-only authentication overrides for managed browser state."""
    if provider == "aws":
        return aws.terraform_auth_environment(environ)
    if provider == "azure":
        return azure.terraform_auth_environment(environ)
    return {}
