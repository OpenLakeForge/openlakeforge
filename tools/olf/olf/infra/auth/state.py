"""Provider-neutral authentication state shared by the AWS and Azure flows."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from olf.deployment.errors import DeploymentPreconditionError


class AuthenticationError(DeploymentPreconditionError):
    """Authentication is absent, expired, or cannot be refreshed."""


def auth_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return the absolute OLF authentication directory.

    Resolved deliberately: these paths are handed to child processes that run
    with a different working directory - Terraform's `AWS_CONFIG_FILE` and
    credential_process, and the Azure bridge directory prepended to `PATH`
    (Terraform `-chdir` switches the process directory). A relative `OLF_HOME`
    would leave both pointing somewhere that does not exist, so managed
    authentication would fail after a successful login.
    """
    raw = (environ or os.environ).get("OLF_HOME")
    home = Path(raw).expanduser() if raw else Path.home() / ".openlakeforge"
    return (home / "auth").resolve()


def _state_path(provider: str, environ: Mapping[str, str] | None = None) -> Path:
    return auth_home(environ) / f"{provider}.json"


def load_state(provider: str, environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    path = _state_path(provider, environ)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthenticationError(
            f"invalid {provider} authentication state; run 'olf auth login --provider {provider}'."
        ) from exc
    if not isinstance(loaded, dict):
        raise AuthenticationError(
            f"invalid {provider} authentication state; run 'olf auth login --provider {provider}'."
        )
    return loaded


def save_state(provider: str, state: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> None:
    path = _state_path(provider, environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{provider}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(state), handle, sort_keys=True)
            handle.write("\n")
        os.chmod(raw_path, stat.S_IRUSR | stat.S_IWUSR)
        Path(raw_path).replace(path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        Path(raw_path).unlink(missing_ok=True)
        raise


def clear_state(provider: str, environ: Mapping[str, str] | None = None) -> None:
    home = auth_home(environ)
    _state_path(provider, environ).unlink(missing_ok=True)
    if provider == "aws":
        (home / "aws-terraform-config").unlink(missing_ok=True)
    elif provider == "azure":
        bridge = home / "azure-terraform-bridge" / "az"
        bridge.unlink(missing_ok=True)
        if bridge.parent.exists() and not any(bridge.parent.iterdir()):
            bridge.parent.rmdir()


def _expires_at(seconds: int | float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=float(seconds))).isoformat()


def _write_private_text(path: Path, contents: str) -> None:
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
