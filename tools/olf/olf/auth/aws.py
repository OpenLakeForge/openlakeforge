"""AWS authentication through IAM Identity Center, and its Terraform credential process."""

from __future__ import annotations

import os
import sys
import time
import webbrowser
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import boto3
from botocore import UNSIGNED
from botocore.config import Config

from olf.auth.state import AuthenticationError, _expires_at, _write_private_text, auth_home, load_state, save_state

_AWS_SCOPE = ["sso:account:access"]
_AWS_AUTOMATION_VARIABLES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
}


def _uses_aws_automation(environ: Mapping[str, str]) -> bool:
    return any(environ.get(name) for name in _AWS_AUTOMATION_VARIABLES)


def _uses_external_aws_profile(environ: Mapping[str, str]) -> bool:
    """Whether an operator selected a shared AWS profile/configuration.

    OLF's own IAM Identity Center Terraform bridge temporarily sets
    ``AWS_CONFIG_FILE`` and ``AWS_PROFILE=openlakeforge``. That pair must
    continue to resolve the saved OLF SSO state, not recursively treat the
    generated credential-process profile as an external choice.
    """
    profile = environ.get("AWS_PROFILE")
    config_file = environ.get("AWS_CONFIG_FILE")
    managed_config = str(auth_home(environ) / "aws-terraform-config")
    return bool(profile or config_file) and not (profile == "openlakeforge" and config_file == managed_config)


def _aws_instance_profile_available() -> bool:
    """Detect an EC2 instance-profile credential the environment cannot show.

    Every other AWS automation source - static keys, IRSA/EKS Pod Identity's
    web identity token, an ECS/EKS-agent container role - sets an environment
    variable `_uses_aws_automation` can see. A bare EC2 instance profile,
    discovered through IMDS, sets none of them; without this check a saved
    OLF browser session would silently outrank the workload identity ADR 0008
    requires to win. Bounded to a short timeout and a single attempt: IMDS is
    unreachable (not merely absent) off EC2, and this must not add a
    multi-second stall to every interactive deploy that has a saved session.
    """
    from botocore.utils import InstanceMetadataFetcher

    try:
        # botocore-stubs types `timeout` as int, but botocore hands it to urllib3,
        # which takes seconds as a float; rounding up to 1 would be the stall above.
        fetcher = InstanceMetadataFetcher(timeout=0.1, num_attempts=1)  # type: ignore[arg-type]
        return bool(fetcher.retrieve_iam_role_credentials())
    except Exception:
        return False


def _sso_client(service: Literal["sso", "sso-oidc"], *, region: str) -> Any:
    """Create an IAM Identity Center client without resolving AWS profiles.

    Both SSO APIs use the bearer token supplied in their request, not SigV4.
    Marking them unsigned also prevents a Terraform credential_process from
    recursively invoking itself when AWS_PROFILE points at that process.

    Built from a fresh `boto3.Session()`, never the bare `boto3.client()`
    module function: that function shares one process-global default
    session, and `botocore.session.Session` permanently caches the config
    file it parses on its first use for the rest of the process's life. If
    anything earlier in the process makes a bare `boto3.client()`/
    `boto3.Session()` call before `AWS_CONFIG_FILE`/`AWS_PROFILE` are set -
    exactly what happens between resolving foundation facts and the
    artifacts phase's `_applied_authentication_environment` overlay - every
    later bare call keeps resolving profiles against the stale config file,
    raising `ProfileNotFound` for a profile the *current* `AWS_CONFIG_FILE`
    genuinely defines. Reproduced directly: a prior bare `boto3.client()`
    call followed by setting `AWS_CONFIG_FILE`/`AWS_PROFILE` breaks a
    subsequent bare call but not a fresh `boto3.Session()`.
    """
    return boto3.Session().client(service, region_name=region, config=Config(signature_version=UNSIGNED))


def _timestamp_or_duration(value: int | float) -> str:
    """Convert AWS's epoch timestamp (or a test fixture duration) to ISO 8601."""
    numeric = float(value)
    if numeric > 1_000_000_000:
        return datetime.fromtimestamp(numeric, UTC).isoformat()
    return _expires_at(numeric)


def _expired(state: Mapping[str, Any], *, skew_seconds: int = 120) -> bool:
    raw = state.get("access_expires_at")
    if not isinstance(raw, str):
        return True
    try:
        return datetime.fromisoformat(raw) <= datetime.now(UTC) + timedelta(seconds=skew_seconds)
    except ValueError:
        return True


def login_aws(
    *,
    profile: str | None = None,
    start_url: str | None = None,
    sso_region: str | None = None,
    account_id: str | None = None,
    role_name: str | None = None,
    open_browser: bool = True,
    environ: Mapping[str, str] | None = None,
    choose: Any | None = None,
) -> dict[str, Any]:
    """Authenticate through IAM Identity Center's official device flow."""
    env = environ or os.environ
    # An explicit browser configuration must win over an ambient profile;
    # this lets a user deliberately replace an expired CLI SSO session.
    chosen_profile = profile or (env.get("AWS_PROFILE") if not start_url else None)
    if chosen_profile:
        try:
            identity = boto3.Session(profile_name=chosen_profile).client("sts").get_caller_identity()
        except Exception as exc:
            raise AuthenticationError(
                f"AWS profile '{chosen_profile}' is unavailable; run 'olf auth login --provider aws' to sign in."
            ) from exc
        state = {"source": "profile", "profile": chosen_profile, "identity": identity}
        save_state("aws", state, env)
        return state

    if not start_url or not sso_region:
        raise AuthenticationError(
            "AWS IAM Identity Center requires --start-url and --sso-region the first time, "
            "or use --profile to adopt an existing AWS profile."
        )
    oidc = _sso_client("sso-oidc", region=sso_region)
    registration = oidc.register_client(clientName="openlakeforge", clientType="public", scopes=_AWS_SCOPE)
    device = oidc.start_device_authorization(
        clientId=registration["clientId"], clientSecret=registration["clientSecret"], startUrl=start_url
    )
    url = device["verificationUriComplete"]
    opened = webbrowser.open(url) if open_browser else False
    if not opened:
        print(f"Open this AWS sign-in page: {device['verificationUri']}")  # noqa: T201
        print(f"Enter code: {device['userCode']}")  # noqa: T201
    deadline = time.monotonic() + int(device["expiresIn"])
    interval = int(device.get("interval", 5))
    while True:
        if time.monotonic() >= deadline:
            raise AuthenticationError("AWS device authorization expired; run 'olf auth login --provider aws' again.")
        try:
            token = oidc.create_token(
                clientId=registration["clientId"],
                clientSecret=registration["clientSecret"],
                grantType="urn:ietf:params:oauth:grant-type:device_code",
                deviceCode=device["deviceCode"],
            )
            break
        except oidc.exceptions.AuthorizationPendingException:
            time.sleep(interval)
        except oidc.exceptions.SlowDownException:
            interval += 5
            time.sleep(interval)
        except oidc.exceptions.AccessDeniedException as exc:
            raise AuthenticationError("AWS device authorization was denied.") from exc

    sso = _sso_client("sso", region=sso_region)
    accounts = list(sso.get_paginator("list_accounts").paginate(accessToken=token["accessToken"]))
    flattened_accounts = [account for page in accounts for account in page["accountList"]]
    selected_account = _resolve_choice(account_id, flattened_accounts, "accountId", choose, "AWS account")
    roles = list(
        sso.get_paginator("list_account_roles").paginate(accessToken=token["accessToken"], accountId=selected_account)
    )
    flattened_roles = [role for page in roles for role in page["roleList"]]
    selected_role = _resolve_choice(role_name, flattened_roles, "roleName", choose, "AWS role")
    state = {
        "source": "olf-sso",
        "start_url": start_url,
        "sso_region": sso_region,
        "account_id": selected_account,
        "role_name": selected_role,
        "client_id": registration["clientId"],
        "client_secret": registration["clientSecret"],
        "client_secret_expires_at": _timestamp_or_duration(registration.get("clientSecretExpiresAt", 0)),
        "access_token": token["accessToken"],
        "access_expires_at": _expires_at(token["expiresIn"]),
        "refresh_token": token.get("refreshToken", ""),
    }
    save_state("aws", state, env)
    return state


def _resolve_choice(
    explicit: str | None, items: list[Mapping[str, Any]], key: str, choose: Any | None, label: str
) -> str:
    """Return the caller's explicit selection, or prompt for one.

    An explicit `--account-id`/`--role-name` is validated against what the
    session actually offers. Persisting an unlisted value would report
    "authentication ready" and then fail much later inside
    `get_role_credentials`, where the cause is far from obvious.
    """
    if not explicit:
        return _choose(items, key, choose, label)
    available = [str(item[key]) for item in items if item.get(key)]
    if explicit not in available:
        offered = ", ".join(sorted(available)) or "(none)"
        raise AuthenticationError(f"{label} {explicit!r} is not available for this session; offered: {offered}.")
    return explicit


def _choose(items: list[Mapping[str, Any]], key: str, choose: Any | None, label: str) -> str:
    values = [str(item[key]) for item in items if item.get(key)]
    if len(values) == 1:
        return values[0]
    if not values:
        raise AuthenticationError(f"No {label}s are available for this account.")
    if choose is None:
        raise AuthenticationError(f"Multiple {label}s are available; select one with the corresponding option.")
    return str(choose(values, label))


def aws_session(environ: Mapping[str, str], *, region: str | None = None) -> Any:
    """Return a boto3 session sourced from OLF state or normal SDK discovery."""
    if _uses_aws_automation(environ) or _uses_external_aws_profile(environ):
        # An explicit AWS_PROFILE disables botocore's environment-credential
        # provider, so injected automation keys/web-identity tokens would be
        # silently ignored. Let botocore's own chain apply normal precedence.
        return boto3.Session(region_name=region)
    state = load_state("aws", environ)
    if state is None:
        return boto3.Session(profile_name=environ.get("AWS_PROFILE"), region_name=region)
    if _aws_instance_profile_available():
        return boto3.Session(region_name=region)
    if state.get("source") == "profile":
        return boto3.Session(profile_name=str(state["profile"]), region_name=region)
    if state.get("source") != "olf-sso":
        raise AuthenticationError("unknown AWS authentication source; run 'olf auth login --provider aws'.")
    state = _refresh_aws_access_token(state, environ)
    sso = _sso_client("sso", region=str(state["sso_region"]))
    credentials = sso.get_role_credentials(
        roleName=str(state["role_name"]), accountId=str(state["account_id"]), accessToken=str(state["access_token"])
    )["roleCredentials"]
    return boto3.Session(
        aws_access_key_id=credentials["accessKeyId"],
        aws_secret_access_key=credentials["secretAccessKey"],
        aws_session_token=credentials["sessionToken"],
        region_name=region,
    )


def aws_process_credentials(environ: Mapping[str, str]) -> dict[str, Any]:
    """Return AWS `credential_process`-protocol JSON with a real expiry.

    For `olf-sso` state this calls `get_role_credentials` directly instead of
    going through `aws_session`, so the SSO API's own `expiration` (epoch
    milliseconds) reaches Terraform - that session's permission-set duration
    can be shorter than a guessed TTL, and botocore rejects credentials whose
    reported expiry has already passed. Every other source has no exposed
    expiry, so a short, conservative TTL is used instead: it is safe to be
    wrong short (Terraform just re-invokes sooner) but unsafe to be wrong long
    (botocore trusts a stale `Expiration` for its full stated duration).
    """
    state = load_state("aws", environ)
    if state is not None and state.get("source") == "olf-sso":
        state = _refresh_aws_access_token(dict(state), environ)
        sso = _sso_client("sso", region=str(state["sso_region"]))
        credentials = sso.get_role_credentials(
            roleName=str(state["role_name"]),
            accountId=str(state["account_id"]),
            accessToken=str(state["access_token"]),
        )["roleCredentials"]
        return {
            "Version": 1,
            "AccessKeyId": credentials["accessKeyId"],
            "SecretAccessKey": credentials["secretAccessKey"],
            "SessionToken": credentials["sessionToken"],
            "Expiration": datetime.fromtimestamp(credentials["expiration"] / 1000, UTC).isoformat(),
        }
    session = aws_session(environ)
    frozen = session.get_credentials().get_frozen_credentials()
    return {
        "Version": 1,
        "AccessKeyId": frozen.access_key,
        "SecretAccessKey": frozen.secret_key,
        "SessionToken": frozen.token,
        "Expiration": _expires_at(300),
    }


def _refresh_aws_access_token(state: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    if not _expired(state):
        return state
    refresh_token = state.get("refresh_token")
    if not refresh_token:
        raise AuthenticationError("AWS SSO session expired; run 'olf auth login --provider aws'.")
    oidc = _sso_client("sso-oidc", region=str(state["sso_region"]))
    try:
        token = oidc.create_token(
            clientId=str(state["client_id"]),
            clientSecret=str(state["client_secret"]),
            grantType="refresh_token",
            refreshToken=str(refresh_token),
        )
    except Exception as exc:
        raise AuthenticationError("AWS SSO session expired; run 'olf auth login --provider aws'.") from exc
    state["access_token"] = token["accessToken"]
    state["access_expires_at"] = _expires_at(token["expiresIn"])
    if token.get("refreshToken"):
        state["refresh_token"] = token["refreshToken"]
    save_state("aws", state, environ)
    return state


def terraform_auth_environment(environ: Mapping[str, str]) -> dict[str, str]:
    if _uses_aws_automation(environ) or _uses_external_aws_profile(environ):
        return {}
    state = load_state("aws", environ)
    if state is None:
        return {}
    if _aws_instance_profile_available():
        return {}
    if state.get("source") == "olf-sso":
        config_path = auth_home(environ) / "aws-terraform-config"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        command = f'"{sys.executable}" -m olf.aws_credential_process'
        _write_private_text(config_path, "[profile openlakeforge]\ncredential_process = " + command + "\n")
        return {"AWS_CONFIG_FILE": str(config_path), "AWS_PROFILE": "openlakeforge"}
    if state.get("source") == "profile":
        return {"AWS_PROFILE": str(state["profile"])}
    return {}
