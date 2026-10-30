"""The run-launch contract `olf` and the project-code runtime share."""

from __future__ import annotations

# A Dagster run tag rather than run config: a tag reaches every asset of the
# job (Bronze, Floe's own Silver assets, dbt) without each one declaring a
# config schema, and the launchpad and `olf product run` set it the same way.
FULL_REFRESH_TAG = "openlakeforge/full_refresh"

# `bronze` reloads Bronze from scratch and rebuilds Gold from the Silver it
# merges into; `gold` rebuilds Gold alone and leaves Bronze and Silver as they
# are. There is no Silver-only scope: incremental Bronze keeps each batch, so
# Silver can only be rebuilt from a Bronze that was reloaded whole.
FULL_REFRESH_SCOPES = ("bronze", "gold")


def full_refresh_scope(tags: dict[str, str] | None) -> str | None:
    """The full-refresh scope a run's tags ask for, or None for an incremental run."""
    scope = (tags or {}).get(FULL_REFRESH_TAG)
    if scope is None:
        return None
    if scope not in FULL_REFRESH_SCOPES:
        raise ValueError(f"run tag {FULL_REFRESH_TAG}={scope!r} must be one of {list(FULL_REFRESH_SCOPES)!r}")
    return scope
