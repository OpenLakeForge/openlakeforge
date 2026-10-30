"""Shared `${VAR:-default}`-style environment parsing for deployment settings.

Every provider's `*DeploymentConfig` (`local`, and the `aws`/`azure` cloud
provider) builds its settings dataclasses from `Mapping[str, str]` with the
same bash-style fallback semantics; this is the one implementation both
share.
"""

from __future__ import annotations

from collections.abc import Mapping

from olf.tooling.retry import RetryPolicy


def env(environ: Mapping[str, str], name: str, default: str) -> str:
    value = environ.get(name)
    return default if value is None or value == "" else value


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def int_env(environ: Mapping[str, str], name: str, default: int) -> int:
    return int(env(environ, name, str(default)))


def float_env(environ: Mapping[str, str], name: str, default: float) -> float:
    return float(env(environ, name, str(default)))


def retry_policy(
    environ: Mapping[str, str],
    *,
    specific_attempts: str,
    specific_delay: str,
    generic_attempts: str = "DOCKER_REGISTRY_ATTEMPTS",
    generic_delay: str = "DOCKER_REGISTRY_RETRY_DELAY_SECONDS",
    default_attempts: int = 3,
    default_delay: float = 10.0,
) -> RetryPolicy:
    """Port of `scripts/lib/docker.sh`'s two-tier `SPECIFIC:-GENERIC:-default` fallback."""
    attempts = int_env(environ, specific_attempts, int_env(environ, generic_attempts, default_attempts))
    delay = float_env(environ, specific_delay, float_env(environ, generic_delay, default_delay))
    return RetryPolicy(max_attempts=attempts, delay_seconds=delay)
