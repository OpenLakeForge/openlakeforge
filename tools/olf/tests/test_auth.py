from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from typer.testing import CliRunner

from olf import auth
from olf.azure_bridge import main as azure_bridge_main
from olf.cli import app


class _Pager:
    def __init__(self, pages):  # noqa: ANN001
        self.pages = pages

    def paginate(self, **_kwargs):  # noqa: ANN003, ANN202
        return self.pages


class _Oidc:
    class exceptions:
        class AuthorizationPendingException(Exception): ...

        class SlowDownException(Exception): ...

        class AccessDeniedException(Exception): ...

    def register_client(self, **_kwargs):  # noqa: ANN003, ANN202
        return {"clientId": "client", "clientSecret": "secret", "clientSecretExpiresAt": 3600}

    def start_device_authorization(self, **_kwargs):  # noqa: ANN003, ANN202
        return {
            "verificationUri": "https://device.aws",
            "verificationUriComplete": "https://device.aws/?code=abc",
            "userCode": "ABC",
            "deviceCode": "device",
            "expiresIn": 60,
            "interval": 1,
        }

    def create_token(self, **_kwargs):  # noqa: ANN003, ANN202
        return {"accessToken": "access", "refreshToken": "refresh", "expiresIn": 3600}


class _Sso:
    def get_paginator(self, name: str):  # noqa: ANN201
        if name == "list_accounts":
            return _Pager([{"accountList": [{"accountId": "123"}]}])
        return _Pager([{"roleList": [{"roleName": "Administrator"}]}])


class _PollingOidc(_Oidc):
    def __init__(self, outcomes):  # noqa: ANN001
        self.outcomes = iter(outcomes)

    def create_token(self, **_kwargs):  # noqa: ANN003, ANN202
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_aws_login_opens_only_the_aws_provided_url_and_saves_private_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: list[str] = []
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    monkeypatch.setattr(
        boto3,
        "Session",
        lambda *_a, **_k: SimpleNamespace(
            client=lambda service, **_kwargs: _Oidc() if service == "sso-oidc" else _Sso()
        ),
    )
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    state = auth.login_aws(start_url="https://portal.awsapps.com/start", sso_region="eu-west-1")

    assert opened == ["https://device.aws/?code=abc"]
    assert state["account_id"] == "123"
    saved = tmp_path / "auth" / "aws.json"
    assert saved.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    assert auth.load_state("aws")["refresh_token"] == "refresh"


def test_aws_login_without_browser_prints_only_aws_device_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    monkeypatch.setattr(
        boto3,
        "Session",
        lambda *_a, **_k: SimpleNamespace(
            client=lambda service, **_kwargs: _Oidc() if service == "sso-oidc" else _Sso()
        ),
    )

    auth.login_aws(start_url="https://portal.awsapps.com/start", sso_region="eu-west-1", open_browser=False)

    output = capsys.readouterr().out
    assert "https://device.aws" in output
    assert "ABC" in output


def test_aws_device_polling_retries_pending_and_slowdown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    oidc = _PollingOidc(
        [
            _Oidc.exceptions.AuthorizationPendingException(),
            _Oidc.exceptions.SlowDownException(),
            {"accessToken": "access", "refreshToken": "refresh", "expiresIn": 3600},
        ]
    )
    pauses: list[int] = []
    monkeypatch.setattr(
        boto3,
        "Session",
        lambda *_a, **_k: SimpleNamespace(
            client=lambda service, **_kwargs: oidc if service == "sso-oidc" else _Sso()
        ),
    )
    monkeypatch.setattr(time, "sleep", pauses.append)

    auth.login_aws(start_url="https://portal.awsapps.com/start", sso_region="eu-west-1", open_browser=False)

    assert pauses == [1, 6]


def test_aws_device_polling_reports_denial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    oidc = _PollingOidc([_Oidc.exceptions.AccessDeniedException()])
    monkeypatch.setattr(
        boto3,
        "Session",
        lambda *_a, **_k: SimpleNamespace(
            client=lambda service, **_kwargs: oidc if service == "sso-oidc" else _Sso()
        ),
    )

    with pytest.raises(auth.AuthenticationError, match="denied"):
        auth.login_aws(start_url="https://portal.awsapps.com/start", sso_region="eu-west-1", open_browser=False)


def test_auth_status_redacts_tokens_and_logout_only_removes_olf_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    auth.save_state("aws", {"source": "olf-sso", "access_token": "access", "refresh_token": "refresh"})
    runner = CliRunner()

    status = runner.invoke(app, ["auth", "status", "--provider", "aws"])
    logout = runner.invoke(app, ["auth", "logout", "--provider", "aws"])

    assert status.exit_code == 0
    assert "access" not in status.output
    assert logout.exit_code == 0
    assert auth.load_state("aws") is None


def test_azure_status_translates_an_expired_sdk_token_into_a_clean_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the saved Azure state's SDK token cache is expired, unavailable,
    or corrupt, `get_token()` raises an azure-core/azure-identity exception,
    not this project's `AuthenticationError`. `status` must translate it into
    the same actionable CLI error used for every other authentication
    failure - not let a raw traceback surface for the common re-login case.
    """
    from azure.core.exceptions import ClientAuthenticationError

    import olf.commands.auth as auth_command

    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    auth.save_state("azure", {"source": "azure-cli", "subscription_id": "sub-id"})

    class _ExpiredCredential:
        def get_token(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            raise ClientAuthenticationError(message="token cache expired")

    monkeypatch.setattr(auth_command, "azure_credential", lambda *_args: _ExpiredCredential())
    runner = CliRunner()

    result = runner.invoke(app, ["auth", "status", "--provider", "azure"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "olf auth login --provider azure" in result.output


def test_terraform_auth_environment_keeps_automation_ahead_of_saved_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    auth.save_state("aws", {"source": "olf-sso", "access_token": "access"})

    assert auth.terraform_auth_environment("aws", {"OLF_HOME": str(tmp_path), "AWS_ACCESS_KEY_ID": "automation"}) == {}


def test_aws_session_does_not_force_a_profile_when_automation_credentials_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `profile_name` disables botocore's environment-credential
    provider, so passing the ambient `AWS_PROFILE` through would make CI/
    workload-identity keys silently unreachable - Terraform (which never
    sees a profile override for automation, see the test above) and the SDK
    adapters would then authenticate as two different identities.
    """
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        boto3, "Session", lambda **kwargs: calls.append(kwargs) or object()
    )

    auth.aws_session({"AWS_ACCESS_KEY_ID": "automation", "AWS_PROFILE": "company-sso"}, region="eu-west-1")

    assert calls == [{"region_name": "eu-west-1"}]


def test_explicit_aws_profile_takes_precedence_over_saved_olf_sso(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {"OLF_HOME": str(tmp_path), "AWS_PROFILE": "company-sso"}
    auth.save_state("aws", {"source": "olf-sso", "access_token": "access"}, env)
    monkeypatch.setattr(auth.aws, "_aws_instance_profile_available", lambda: False)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: calls.append(kwargs) or object())

    auth.aws_session(env, region="eu-west-1")

    assert calls == [{"region_name": "eu-west-1"}]
    assert auth.terraform_auth_environment("aws", env) == {}


def test_olf_generated_credential_process_profile_remains_managed(tmp_path: Path) -> None:
    env = {
        "OLF_HOME": str(tmp_path),
        "AWS_PROFILE": "openlakeforge",
        "AWS_CONFIG_FILE": str(tmp_path / "auth" / "aws-terraform-config"),
    }

    assert not auth.aws._uses_external_aws_profile(env)  # noqa: SLF001


def test_credential_selection_environment_excludes_unrelated_values() -> None:
    assert auth.credential_selection_environment(
        "aws",
        {
            "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/token",
            "AWS_ROLE_ARN": "arn:aws:iam::123:role/ci",
            "OLF_HOME": "/tmp/olf-home",
            "HOME": "/home",
        },
    ) == {
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/token",
        "AWS_ROLE_ARN": "arn:aws:iam::123:role/ci",
        "OLF_HOME": "/tmp/olf-home",
    }


def test_terraform_auth_environment_propagates_adopted_provider_selections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    env = {"OLF_HOME": str(tmp_path)}
    auth.save_state("aws", {"source": "profile", "profile": "company-sso"}, env)
    auth.save_state("azure", {"source": "azure-cli", "subscription_id": "sub-id", "tenant_id": "tenant-id"}, env)

    assert auth.terraform_auth_environment("aws", env) == {"AWS_PROFILE": "company-sso"}
    assert auth.terraform_auth_environment("azure", env) == {
        "ARM_SUBSCRIPTION_ID": "sub-id",
        "ARM_TENANT_ID": "tenant-id",
    }


def test_azure_terraform_bridge_supports_only_azurerm_command_contract(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    subscription = type(
        "Subscription",
        (),
        {"subscription_id": "sub-id", "display_name": "Sandbox", "tenant_id": "tenant-id", "state": "Enabled"},
    )()
    credential = type(
        "Credential", (), {"get_token": lambda *_args: type("Token", (), {"token": "secret", "expires_on": 1})()}
    )()
    import olf.azure_bridge as bridge

    monkeypatch.setattr(
        bridge,
        "load_state",
        lambda *_args: {"source": "olf-browser", "subscription_id": "sub-id", "tenant_id": "tenant-id"},
    )
    monkeypatch.setattr(bridge, "azure_credential", lambda *_args: credential)
    monkeypatch.setattr(
        bridge,
        "SubscriptionClient",
        lambda *_args: type(
            "Client", (), {"subscriptions": type("Subscriptions", (), {"list": lambda *_args: [subscription]})()}
        )(),
    )
    monkeypatch.setattr("sys.argv", ["az", "account", "show", "-o=json"])

    azure_bridge_main()

    assert json.loads(capsys.readouterr().out)["id"] == "sub-id"


def test_azure_browser_login_delegates_to_azure_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import azure.identity
    import azure.mgmt.resource.subscriptions

    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    calls: list[object] = []
    record = type("Record", (), {"serialize": lambda *_args: "record", "username": "user@example.com"})()
    credential = type("Credential", (), {"authenticate": lambda *_args, **_kwargs: calls.append(True) or record})()
    subscription = type(
        "Subscription",
        (),
        {"subscription_id": "sub-id", "display_name": "Sandbox", "tenant_id": "tenant-id", "state": "Enabled"},
    )()
    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", lambda **_kwargs: credential)
    monkeypatch.setattr(
        azure.mgmt.resource.subscriptions,
        "SubscriptionClient",
        lambda *_args: type(
            "Client", (), {"subscriptions": type("Subscriptions", (), {"list": lambda *_args: [subscription]})()}
        )(),
    )

    state = auth.login_azure()

    assert calls == [True]
    assert state["principal"] == "user@example.com"


def test_azure_device_code_login_allows_headless_sdk_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import azure.identity
    import azure.mgmt.resource.subscriptions

    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    options: list[dict[str, object]] = []
    record = type("Record", (), {"serialize": lambda *_args: "record", "username": "user@example.com"})()
    credential = type("Credential", (), {"authenticate": lambda *_args, **_kwargs: record})()
    subscription = type(
        "Subscription",
        (),
        {"subscription_id": "sub-id", "display_name": "Sandbox", "tenant_id": "tenant-id", "state": "Enabled"},
    )()
    monkeypatch.setattr(
        azure.identity,
        "TokenCachePersistenceOptions",
        lambda **kwargs: options.append(kwargs) or object(),
    )
    monkeypatch.setattr(azure.identity, "DeviceCodeCredential", lambda **_kwargs: credential)
    monkeypatch.setattr(
        azure.mgmt.resource.subscriptions,
        "SubscriptionClient",
        lambda *_args: type(
            "Client", (), {"subscriptions": type("Subscriptions", (), {"list": lambda *_args: [subscription]})()}
        )(),
    )

    auth.login_azure(device_code=True)

    assert options == [{"name": "openlakeforge-auth", "allow_unencrypted_storage": True}]


def test_aws_process_credentials_carries_the_real_sso_expiration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SSO API's `get_role_credentials` returns its own `expiration`
    (epoch milliseconds) - a permission set's session duration can be
    shorter than any TTL this module might guess, and botocore rejects
    `credential_process` output whose reported `Expiration` has already
    passed.
    """
    env = {"OLF_HOME": str(tmp_path)}
    auth.save_state(
        "aws",
        {
            "source": "olf-sso",
            "sso_region": "eu-west-1",
            "account_id": "123",
            "role_name": "Administrator",
            "access_token": "access",
            "access_expires_at": auth.state._expires_at(3600),
        },
        env,
    )
    sso = type(
        "Sso",
        (),
        {
            "get_role_credentials": lambda *_args, **_kwargs: {
                "roleCredentials": {
                    "accessKeyId": "AKIA",
                    "secretAccessKey": "secret",
                    "sessionToken": "token",
                    "expiration": 1_700_000_000_000,
                }
            }
        },
    )()
    monkeypatch.setattr(
        boto3, "Session", lambda *_a, **_k: SimpleNamespace(client=lambda _service, **_kwargs: sso)
    )

    credentials = auth.aws_process_credentials(env)

    assert credentials["AccessKeyId"] == "AKIA"
    assert credentials["Expiration"] == "2023-11-14T22:13:20+00:00"


def test_aws_process_credentials_falls_back_to_a_short_ttl_without_sso_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No exposed expiry exists for a profile/automation-sourced session, so a
    short TTL is used instead of guessing a long one: wrong-short just makes
    Terraform re-invoke sooner, but wrong-long risks botocore trusting a
    stale `Expiration` for its full duration.
    """
    env = {"OLF_HOME": str(tmp_path)}
    credentials_obj = type(
        "Credentials",
        (),
        {
            "get_frozen_credentials": lambda self: type(
                "Frozen", (), {"access_key": "AKIA", "secret_key": "secret", "token": "token"}
            )()
        },
    )()
    session = type("Session", (), {"get_credentials": lambda self: credentials_obj})()
    monkeypatch.setattr(auth.aws, "aws_session", lambda *_args, **_kwargs: session)

    credentials = auth.aws_process_credentials(env)

    assert credentials["AccessKeyId"] == "AKIA"
    assert credentials["Expiration"] > auth.state._expires_at(0)


def test_azure_bridge_tolerates_azurerm_option_flags_on_account_commands(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`go-azure-sdk`'s `jsonUnmarshalAzCmd` appends `-o=json` and, on its
    subscription-aware path, `--subscription <id>`. Those flags are not part
    of the command identity, so the bridge must match on the leading verb
    pair - otherwise an AzureRM bump silently breaks Terraform Azure auth.
    """
    subscription = type(
        "Subscription",
        (),
        {"subscription_id": "sub-id", "display_name": "Sandbox", "tenant_id": "tenant-id", "state": "Enabled"},
    )()
    import olf.azure_bridge as bridge

    monkeypatch.setattr(
        bridge,
        "load_state",
        lambda *_args: {"source": "olf-browser", "subscription_id": "sub-id", "tenant_id": "tenant-id"},
    )
    monkeypatch.setattr(bridge, "azure_credential", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "SubscriptionClient",
        lambda *_args: type(
            "Client", (), {"subscriptions": type("Subscriptions", (), {"list": lambda *_args: [subscription]})()}
        )(),
    )
    monkeypatch.setattr("sys.argv", ["az", "account", "show", "--subscription", "sub-id", "-o=json"])

    azure_bridge_main()

    assert json.loads(capsys.readouterr().out)["id"] == "sub-id"


def test_azure_bridge_rejects_unsupported_commands_before_resolving_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """ADR 0008 binds the bridge to four commands. Anything else must be
    refused with that message and without a credential/network call, so the
    operator sees the contract violation rather than an unrelated auth error.
    """
    import olf.azure_bridge as bridge

    def _fail(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("credentials must not be resolved for an unsupported command")

    monkeypatch.setattr(bridge, "load_state", _fail)
    monkeypatch.setattr(bridge, "azure_credential", _fail)
    monkeypatch.setattr(bridge, "SubscriptionClient", _fail)
    monkeypatch.setattr("sys.argv", ["az", "group", "delete", "--name", "rg"])

    with pytest.raises(SystemExit):
        azure_bridge_main()

    assert "unsupported Azure CLI command" in capsys.readouterr().err


def test_auth_home_is_absolute_for_a_relative_olf_home() -> None:
    """`AWS_CONFIG_FILE` and the Azure bridge PATH entry are handed to child
    processes that run from a different directory (Terraform `-chdir` switches
    the process directory), so a relative `OLF_HOME` must not leak through.
    """
    resolved = auth.auth_home({"OLF_HOME": "relative-home"})

    assert resolved.is_absolute()
    assert resolved.name == "auth"


def test_aws_login_rejects_a_role_the_session_does_not_offer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo'd `--role-name` must fail at login, not much later inside
    `get_role_credentials` where the cause is unrecognisable.
    """
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    monkeypatch.setattr(
        boto3,
        "Session",
        lambda *_a, **_k: SimpleNamespace(
            client=lambda service, **_kwargs: _Oidc() if service == "sso-oidc" else _Sso()
        ),
    )

    with pytest.raises(auth.AuthenticationError, match="not available for this session"):
        auth.login_aws(
            start_url="https://portal.awsapps.com/start",
            sso_region="eu-west-1",
            role_name="Adminstrator",  # codespell:ignore - deliberate typo under test
            open_browser=False,
        )

    assert auth.load_state("aws") is None


def test_aws_login_accepts_an_offered_role_without_prompting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    monkeypatch.setattr(
        boto3,
        "Session",
        lambda *_a, **_k: SimpleNamespace(
            client=lambda service, **_kwargs: _Oidc() if service == "sso-oidc" else _Sso()
        ),
    )

    state = auth.login_aws(
        start_url="https://portal.awsapps.com/start",
        sso_region="eu-west-1",
        account_id="123",
        role_name="Administrator",
        open_browser=False,
    )

    assert state["role_name"] == "Administrator"
    assert state["account_id"] == "123"


def test_aws_instance_profile_takes_precedence_over_a_saved_browser_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An EC2 instance-profile credential, discovered through IMDS, sets no
    environment variable `_uses_aws_automation` can see - unlike every other
    AWS automation source. Without checking IMDS directly, a saved OLF
    browser session on the same host would silently outrank the workload
    identity ADR 0008 requires to win, breaking unattended deployment when
    the session is stale or deploying as the wrong principal when it is not.
    """
    env = {"OLF_HOME": str(tmp_path)}
    auth.save_state("aws", {"source": "profile", "profile": "stale-dev-profile"}, env)
    monkeypatch.setattr(auth.aws, "_aws_instance_profile_available", lambda: True)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: calls.append(kwargs) or object())

    auth.aws_session(env)

    assert calls == [{"region_name": None}]


def test_aws_saved_session_wins_when_no_instance_profile_is_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {"OLF_HOME": str(tmp_path)}
    auth.save_state("aws", {"source": "profile", "profile": "my-profile"}, env)
    monkeypatch.setattr(auth.aws, "_aws_instance_profile_available", lambda: False)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: calls.append(kwargs) or object())

    auth.aws_session(env)

    assert calls == [{"profile_name": "my-profile", "region_name": None}]


def test_terraform_auth_environment_defers_to_an_aws_instance_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {"OLF_HOME": str(tmp_path)}
    auth.save_state("aws", {"source": "olf-sso", "access_token": "access"}, env)
    monkeypatch.setattr(auth.aws, "_aws_instance_profile_available", lambda: True)

    assert auth.terraform_auth_environment("aws", env) == {}


def test_azure_managed_identity_takes_precedence_over_a_saved_browser_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The VM/VMSS system-assigned managed identity equivalent of the AWS
    instance-profile gap above: discovered through IMDS, sets nothing
    `_uses_azure_automation` can see.
    """
    env = {"OLF_HOME": str(tmp_path)}
    auth.save_state("azure", {"source": "azure-cli", "subscription_id": "sub-id"}, env)
    monkeypatch.setattr(auth.azure, "_azure_managed_identity_available", lambda: True)

    credential = auth.azure_credential(env)

    assert type(credential).__name__ == "DefaultAzureCredential"


def test_user_assigned_azure_managed_identity_takes_precedence_over_saved_browser_session(
    tmp_path: Path,
) -> None:
    env = {
        "OLF_HOME": str(tmp_path),
        "ARM_USE_MSI": "true",
        "ARM_CLIENT_ID": "user-assigned-client-id",
    }
    auth.save_state("azure", {"source": "azure-cli", "subscription_id": "sub-id"}, env)

    credential = auth.azure_credential(env)

    assert type(credential).__name__ == "ManagedIdentityCredential"
    assert auth.azure._uses_azure_automation(env)  # noqa: SLF001
    assert auth.terraform_auth_environment("azure", env) == {}


def test_azure_client_id_selects_a_user_assigned_managed_identity() -> None:
    credential = auth.azure_credential({"AZURE_CLIENT_ID": "user-assigned-client-id"})

    assert type(credential).__name__ == "ManagedIdentityCredential"


def test_terraform_auth_environment_defers_to_an_azure_managed_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {"OLF_HOME": str(tmp_path)}
    auth.save_state("azure", {"source": "azure-cli", "subscription_id": "sub-id"}, env)
    monkeypatch.setattr(auth.azure, "_azure_managed_identity_available", lambda: True)

    assert auth.terraform_auth_environment("azure", env) == {}


def test_aws_instance_profile_probe_is_bounded_and_offline_safe() -> None:
    """Must not add a multi-second stall to a normal interactive deploy when
    IMDS is unreachable (every non-EC2 host, including every developer
    laptop and CI runner without an instance profile).
    """
    import time

    start = time.monotonic()
    available = auth.aws._aws_instance_profile_available()  # noqa: SLF001
    elapsed = time.monotonic() - start

    assert available is False
    assert elapsed < 3.0


def test_sso_client_survives_a_config_file_set_after_an_earlier_bare_boto3_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`boto3.client()` (the bare module function) shares one process-global
    default session, and `botocore.session.Session` permanently caches the
    config file it parses on first use. `olf.deployment.cloud.artifacts`
    resolves foundation facts (an earlier `aws_session` call, which can reach
    `_sso_client`) before its `_applied_authentication_environment` overlay
    sets `AWS_CONFIG_FILE`/`AWS_PROFILE` for the artifacts phase - reproduced
    directly against a real Terraform-style config file: an earlier bare
    `boto3.client()` call makes a later one raise `ProfileNotFound` for a
    profile the *current* config file genuinely defines, while a fresh
    `boto3.Session()` per call (what `_sso_client` now does) is unaffected.
    """
    # Isolate from whatever the real process-global default session already
    # is (from an earlier test, or nothing) so this reproduces the bug from a
    # known-clean baseline rather than depending on test execution order.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    config_dir = tempfile.mkdtemp()
    config_path = os.path.join(config_dir, "aws-terraform-config")
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("[profile openlakeforge]\nregion = eu-west-1\n")

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE")
    boto3.client("sts", region_name="eu-west-1", config=Config(signature_version=UNSIGNED))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")

    monkeypatch.setenv("AWS_PROFILE", "openlakeforge")
    monkeypatch.setenv("AWS_CONFIG_FILE", config_path)

    client = auth.aws._sso_client("sso", region="eu-west-1")  # noqa: SLF001 - regression coverage for the fix itself.

    assert client is not None


def test_azure_arm_client_secret_automation_is_detected_and_translated() -> None:
    """AzureRM's provider reads ARM_CLIENT_ID/ARM_CLIENT_SECRET/ARM_TENANT_ID
    directly and never goes through azure-identity, so Terraform authenticates
    correctly with only those set while `DefaultAzureCredential` - which only
    recognizes the differently-named AZURE_* forms - sees no automation
    source at all and would fall through to a saved browser session.
    """
    env = {"ARM_CLIENT_ID": "client-id", "ARM_TENANT_ID": "tenant-id", "ARM_CLIENT_SECRET": "secret"}

    assert auth.azure._uses_azure_automation(env)  # noqa: SLF001
    credential = auth.azure_credential(env)

    assert type(credential).__name__ == "ClientSecretCredential"


def test_azure_arm_certificate_automation_is_detected_and_translated(tmp_path: Path) -> None:
    """`CertificateCredential` reads its certificate file eagerly at
    construction time, so this needs a real (throwaway) PEM, not just a path.
    """
    import subprocess

    cert_path = tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(cert_path), "-out", str(cert_path),
            "-days", "1", "-nodes", "-subj", "/CN=olf-test",
        ],
        check=True,
        capture_output=True,
    )
    env = {"ARM_CLIENT_ID": "client-id", "ARM_TENANT_ID": "tenant-id", "ARM_CLIENT_CERTIFICATE_PATH": str(cert_path)}

    credential = auth.azure_credential(env)

    assert type(credential).__name__ == "CertificateCredential"


def test_azure_arm_oidc_token_automation_is_detected_and_translated() -> None:
    env = {"ARM_CLIENT_ID": "client-id", "ARM_TENANT_ID": "tenant-id", "ARM_OIDC_TOKEN": "jwt-token"}

    credential = auth.azure_credential(env)

    assert type(credential).__name__ == "ClientAssertionCredential"


def test_azure_arm_oidc_token_file_automation_reads_the_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("jwt-from-file\n")
    env = {"ARM_CLIENT_ID": "client-id", "ARM_TENANT_ID": "tenant-id", "ARM_OIDC_TOKEN_FILE_PATH": str(token_file)}

    credential = auth.azure_credential(env)

    assert credential._func() == "jwt-from-file"  # noqa: SLF001 - exercising the assertion callable directly.


def test_azure_arm_automation_requires_both_client_id_and_tenant_id() -> None:
    assert not auth.azure._uses_azure_automation({"ARM_CLIENT_SECRET": "secret"})  # noqa: SLF001
    assert not auth.azure._uses_azure_automation({"ARM_CLIENT_ID": "client-id"})  # noqa: SLF001


def test_azure_browser_login_rejects_an_inaccessible_subscription_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `--subscription-id` typo must raise `AuthenticationError`
    (which the CLI catches) rather than an unhandled `StopIteration` from
    `next()` over an empty match.
    """
    import azure.identity
    import azure.mgmt.resource.subscriptions

    record = type("Record", (), {"serialize": lambda *_args: "record", "username": "user@example.com"})()
    credential = type("Credential", (), {"authenticate": lambda *_args, **_kwargs: record})()
    subscription = type(
        "Subscription", (), {"subscription_id": "sub-id", "display_name": "Sandbox", "tenant_id": "tenant-id"}
    )()
    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", lambda **_kwargs: credential)
    monkeypatch.setattr(
        azure.mgmt.resource.subscriptions,
        "SubscriptionClient",
        lambda *_args: type(
            "Client", (), {"subscriptions": type("Subscriptions", (), {"list": lambda *_args: [subscription]})()}
        )(),
    )

    with pytest.raises(auth.AuthenticationError, match="not available"):
        auth.login_azure(subscription_id="typo-sub-id")


def test_azure_browser_login_accepts_an_offered_subscription_without_prompting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import azure.identity
    import azure.mgmt.resource.subscriptions

    monkeypatch.setenv("OLF_HOME", str(tmp_path))
    record = type("Record", (), {"serialize": lambda *_args: "record", "username": "user@example.com"})()
    credential = type("Credential", (), {"authenticate": lambda *_args, **_kwargs: record})()
    one = type("Subscription", (), {"subscription_id": "sub-id", "display_name": "One", "tenant_id": "tenant-id"})()
    two = type(
        "Subscription", (), {"subscription_id": "other-id", "display_name": "Two", "tenant_id": "tenant-id"}
    )()
    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", lambda **_kwargs: credential)
    monkeypatch.setattr(
        azure.mgmt.resource.subscriptions,
        "SubscriptionClient",
        lambda *_args: type(
            "Client", (), {"subscriptions": type("Subscriptions", (), {"list": lambda *_args: [one, two]})()}
        )(),
    )

    state = auth.login_azure(subscription_id="other-id")

    assert state["subscription_id"] == "other-id"
