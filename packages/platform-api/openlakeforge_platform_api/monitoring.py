"""The monitoring capabilities document (#1259): the stages a caller may monitor, and which sources each stage has.

Built from the settings and the permission matrix alone. Nothing is probed or read here, so a client may ask on every
page load; whether a source answers is the health routes' to say. No reason names a host, URI or bucket.
"""

from __future__ import annotations

from fastapi import Request

from openlakeforge_platform_api import health_routes
from openlakeforge_platform_api.metrics_routes import metrics_configuration
from openlakeforge_platform_api.models import (
    IntegrationStatus,
    MonitoringCapabilities,
    MonitoringCapability,
    MonitoringStage,
)
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.settings import Settings

# The stage-scoped monitoring routes a client may be offered, in display order. A stage on which the caller may call
# none of them is left out of the answer.
MONITORING_OPERATIONS = (
    "get_stage_health",
    "get_stage_service_health",
    "list_pipeline_health",
    "get_run",
    "get_run_failure",
    "get_stage_quality_health",
    "get_run_quality",
    "list_logs",
    "list_stage_metrics",
    "get_stage_metric",
    "list_alert_incidents",
)


def _dagster(settings: Settings, stage: str) -> MonitoringCapability:
    if stage not in settings.dagster_graphql_urls:
        return MonitoringCapability(name="dagster", status="disabled", reason="no Dagster is configured for this stage")
    return MonitoringCapability(name="dagster", status="available", reason="a Dagster webserver is configured")


def _services(request: Request, stage: str) -> MonitoringCapability:
    # The same condition `health_routes._probe` refuses on, so the two never disagree about `disabled`.
    if request.app.state.service_probes is None or stage not in request.app.state.settings.stage_namespaces:
        return MonitoringCapability(name="services", status="disabled", reason="no service probes are configured")
    return MonitoringCapability(name="services", status="available", reason="service probes are configured")


def _logs(request: Request, stage: str) -> MonitoringCapability:
    if stage not in request.app.state.settings.log_archives:
        return MonitoringCapability(
            name="logs", status="not_provided", reason="no log archive is configured for this stage"
        )
    if request.app.state.object_store is None:
        return MonitoringCapability(
            name="logs", status="unavailable", reason="the platform API holds no object-store key for the log archive"
        )
    return MonitoringCapability(name="logs", status="available", reason="a log archive is configured")


def _metrics(settings: Settings) -> MonitoringCapability:
    absent = metrics_configuration(settings)
    if absent is not None:
        return _from(absent)
    return MonitoringCapability(name="metrics", status="available", reason="a metrics backend is configured")


def _alerts(request: Request) -> MonitoringCapability:
    # The same condition `alert_routes.list_alert_incidents` answers `not_provided` on, so the two never disagree.
    if request.app.state.settings.alerts_state_base_uri is None or request.app.state.alert_state_store is None:
        return MonitoringCapability(
            name="alerts",
            status="not_provided",
            reason="no alert evaluator, or no key to read its state, is configured",
        )
    return MonitoringCapability(name="alerts", status="available", reason="the alert evaluator's state is configured")


def _from(status: IntegrationStatus) -> MonitoringCapability:
    return MonitoringCapability(name=status.name, status=status.status, reason=status.reason)


def _capabilities(request: Request, stage: str) -> list[MonitoringCapability]:
    settings: Settings = request.app.state.settings
    return [
        _dagster(settings, stage),
        _services(request, stage),
        _from(health_routes._quality(request, stage)),
        _from(health_routes._quality_sync(request, stage)),
        _logs(request, stage),
        _metrics(settings),
        _alerts(request),
    ]


def monitoring_capabilities(request: Request) -> MonitoringCapabilities:
    settings: Settings = request.app.state.settings
    matrix: PermissionMatrix = request.app.state.permissions
    role: str | None = request.state.principal.role
    stages = []
    for stage in settings.stages:
        operations = [name for name in MONITORING_OPERATIONS if matrix.allows(name, role, stage)]
        if operations:
            retention = settings.log_archive_retention_days if stage in settings.log_archives else None
            stages.append(
                MonitoringStage(
                    stage=stage,
                    operations=operations,
                    capabilities=_capabilities(request, stage),
                    log_retention_days=retention,
                )
            )
    return MonitoringCapabilities(stages=stages)
