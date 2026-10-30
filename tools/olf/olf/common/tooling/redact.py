"""Centralized diagnostic sanitization for command/environment redaction.

This module never touches the real argv or environment passed to
`subprocess.run` — it only produces sanitized copies for logging, error
messages, and other diagnostics so secret values never leak into them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

REDACTED = "<redacted>"

# Names containing (or ending in) any of these markers are treated as
# sensitive, e.g. AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
# AZURE_CLIENT_SECRET, OPENLINEAGE_API_KEY.
_SENSITIVE_NAME_MARKERS = (
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "CLIENT_SECRET",
    "CREDENTIAL",
    "AUTH",
)

# Flags whose following argv token (or `=`-joined value) is a secret.
_SECRET_VALUE_FLAGS = (
    "--password",
    "--token",
    "--access-token",
    "--client-secret",
    "--secret",
    "--api-key",
    "-p",
)


def is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _SENSITIVE_NAME_MARKERS)


def redact_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `env` with sensitive-looking values replaced."""
    return {key: (REDACTED if is_sensitive_env_name(key) else value) for key, value in env.items()}


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Return a copy of `argv` with values following secret flags replaced."""
    redacted: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            redacted.append(REDACTED)
            redact_next = False
            continue

        flag, sep, _value = token.partition("=")
        if sep and flag.lower() in _SECRET_VALUE_FLAGS:
            redacted.append(f"{flag}={REDACTED}")
            continue

        redacted.append(token)
        if token.lower() in _SECRET_VALUE_FLAGS:
            redact_next = True

    return redacted
