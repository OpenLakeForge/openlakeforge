"""Optional platform-layer decisions shared by environment deploy scripts."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def enabled(environ: Mapping[str, str], layer: str) -> bool:
    """Return whether an optional platform layer is enabled by its contract."""
    variables = {
        "analytics": "OPENLAKEFORGE_ANALYTICS_ENABLED",
        "governance": "OPENLAKEFORGE_GOVERNANCE_ENABLED",
    }
    try:
        value = environ.get(variables[layer], "true")
    except KeyError as exc:
        raise ValueError(f"unknown optional layer {layer!r}") from exc
    return value == "true"


def deploy_enabled_artifacts(
    environ: Mapping[str, str],
    *,
    deploy_reports: Callable[[], None],
    deploy_metadata: Callable[[], None],
    report: Callable[[str], None],
) -> None:
    """Deploy artifacts for enabled optional layers and report skipped layers."""
    if enabled(environ, "analytics"):
        report("Deploying product Superset report assets...")
        deploy_reports()
    else:
        report("Skipping Superset report assets: analytics layer is disabled.")

    if enabled(environ, "governance"):
        report("Deploying OpenMetadata governance metadata...")
        deploy_metadata()
    else:
        report("Skipping OpenMetadata governance metadata: governance layer is disabled.")
