"""Local port-forward target selection.

Port of the inline `local-forward` Makefile target. Slim excludes Superset
and OpenMetadata, matching `DeploymentFeatures.analytics_enabled`/
`governance_enabled`.
"""

from __future__ import annotations

from collections.abc import Mapping

from olf.common import log
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.portforward import ForwardSpec, ForwardTarget


def resolve_dagster_webserver_pod(
    tools: Toolkit,
    *,
    namespace: str,
    context: str,
    kubeconfig,  # noqa: ANN001 - Path, avoided import cycle noise
    env: Mapping[str, str] | None,
) -> str | None:
    result = tools.kubectl.get(
        "pods",
        namespace=namespace,
        context=context,
        kubeconfig=kubeconfig,
        output='jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        env=env,
        check=False,
    )
    for line in result.stdout.splitlines():
        if "dagster-webserver" in line:
            return line
    return None


def local_forward_spec(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> ForwardSpec:
    shared = config.context.shared_namespace
    targets: list[ForwardTarget] = [
        ForwardTarget("seaweedfs-s3", "svc/seaweedfs-s3", 9000, 8333, namespace=shared),
        ForwardTarget("polaris", "svc/polaris", 8181, 8181, namespace=shared),
        ForwardTarget("trino", "svc/trino", 8080, 8080, namespace=shared),
    ]

    dagster_pod = resolve_dagster_webserver_pod(
        tools,
        namespace=config.namespace,
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    )
    if dagster_pod is not None:
        targets.append(ForwardTarget("dagster", f"pod/{dagster_pod}", 3000, 80))
    else:
        log.warn("no dagster-webserver pod found; skipping the Dagster port-forward target.")

    if config.features.analytics_enabled:
        targets.append(ForwardTarget("superset", "svc/superset", 8088, 8088))
    if config.features.governance_enabled:
        targets.append(ForwardTarget("openmetadata", "svc/openmetadata", 8585, 8585, namespace=shared))

    targets += [
        ForwardTarget("seaweedfs-filer", "svc/seaweedfs-filer-client", 8888, 8888, namespace=shared),
        ForwardTarget("seaweedfs-master", "svc/seaweedfs-master", 9333, 9333, namespace=shared),
    ]

    banner = [f"Starting port-forwards for stage '{config.context.stage.value}' (Ctrl-C to stop all)..."]
    if dagster_pod is not None:
        banner.append("  Dagster UI:       http://localhost:3000")
    if config.features.analytics_enabled:
        banner.append("  Superset UI:      http://localhost:8088  (admin; password in Secret superset-credentials)")
    if config.features.governance_enabled:
        banner.append(
            "  OpenMetadata UI:  http://localhost:8585  "
            "(admin@open-metadata.org; password in Secret openmetadata-admin)"
        )
    banner += [
        "  Trino UI:         http://localhost:8080",
        "  Polaris API:      http://localhost:8181",
        "  SeaweedFS S3:     http://localhost:9000",
        "  SeaweedFS Filer:  http://localhost:8888",
        "  SeaweedFS Master: http://localhost:9333",
    ]

    return ForwardSpec(
        targets=tuple(targets),
        namespace=config.namespace,
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        banner=tuple(banner),
    )
