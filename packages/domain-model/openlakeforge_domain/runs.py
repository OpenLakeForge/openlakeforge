"""The run-launch contract `olf` and the project-code runtime share."""

from __future__ import annotations

# A Dagster run tag rather than run config: a tag reaches every asset of the
# job (Bronze, Floe's own Silver assets, dbt) without each one declaring a
# config schema, and the launchpad and `olf product run` set it the same way.
FULL_REFRESH_TAG = "openlakeforge/full_refresh"

# `bronze` reloads Bronze from scratch, rebuilds Silver from it and rebuilds
# Gold; `gold` rebuilds Gold alone and leaves Bronze and Silver as they are.
# There is no Silver-only scope: incremental Bronze keeps each batch, so
# Silver can only be rebuilt from a Bronze that was reloaded whole.
FULL_REFRESH_SCOPES = ("bronze", "gold")

# dagster-floe's own tag (0.4.3): it runs the job's Floe entities with
# `floe run --full-refresh`. A `bronze` refresh must carry it, or Silver would
# merge the new extract onto rows the source no longer has.
FLOE_FULL_REFRESH_TAG = "floe/full_refresh"


def full_refresh_tags(scope: str) -> dict[str, str]:
    """The run tags that ask one run for a full refresh of `scope`."""
    if scope not in FULL_REFRESH_SCOPES:
        raise ValueError(f"full refresh scope {scope!r} must be one of {list(FULL_REFRESH_SCOPES)!r}")
    tags = {FULL_REFRESH_TAG: scope}
    if scope == "bronze":
        tags[FLOE_FULL_REFRESH_TAG] = "true"
    return tags


def full_refresh_scope(tags: dict[str, str] | None) -> str | None:
    """The full-refresh scope a run's tags ask for, or None for an incremental run."""
    tags = tags or {}
    scope = tags.get(FULL_REFRESH_TAG)
    if scope is None:
        return None
    if scope not in FULL_REFRESH_SCOPES:
        raise ValueError(f"run tag {FULL_REFRESH_TAG}={scope!r} must be one of {list(FULL_REFRESH_SCOPES)!r}")
    # Checked before Bronze drops anything, so a run launched with only one of
    # the two tags fails instead of leaving Silver unrebuilt.
    if scope == "bronze" and tags.get(FLOE_FULL_REFRESH_TAG) != "true":
        raise ValueError(
            f"run tag {FULL_REFRESH_TAG}=bronze needs {FLOE_FULL_REFRESH_TAG}=true too, so Floe rebuilds Silver; "
            "`olf infra product run --full-refresh bronze` sets both"
        )
    return scope
