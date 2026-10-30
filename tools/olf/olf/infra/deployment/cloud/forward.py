"""Cloud (AWS/Azure) port-forward target selection.

Port of the `aws-forward`/`azure-forward` Makefile targets. Unlike local
(which discovers the running `dagster-webserver` pod by name), cloud always
targets the stable `svc/dagster-dagster-webserver` Service directly - the
shell scripts this replaces do the same.
"""

from __future__ import annotations

import dataclasses

from olf.infra.deployment.cloud.backend import CloudBackend
from olf.infra.deployment.cloud.config import CloudDeploymentConfig
from olf.infra.deployment.portforward import ForwardSpec, ForwardTarget

_BANNER_LINES: dict[str, str] = {
    "seaweedfs-s3": "  SeaweedFS S3:     http://localhost:9000",
    "polaris": "  Polaris API:      http://localhost:8181",
    "trino": "  Trino UI:         http://localhost:8080",
    "dagster": "  Dagster UI:       http://localhost:3000",
    "superset": "  Superset UI:      http://localhost:8088  (admin; password in Secret superset-credentials)",
    "openmetadata": (
        "  OpenMetadata UI:  http://localhost:8585  (admin@open-metadata.org; password in Secret openmetadata-admin)"
    ),
}


def cloud_forward_spec(config: CloudDeploymentConfig, backend: CloudBackend, *, kube_context: str) -> ForwardSpec:
    shared = config.context.shared_namespace
    # `forward_base_targets()` is platform-level infra (Trino, plus Azure's
    # SeaweedFS/Polaris) - always shared-namespace services, not stage-scoped
    # ones, so every target it returns is rewritten to carry that namespace.
    targets: list[ForwardTarget] = [
        *(dataclasses.replace(target, namespace=shared) for target in backend.forward_base_targets()),
        ForwardTarget("dagster", "svc/dagster-dagster-webserver", 3000, 80),
    ]
    if config.features.analytics_enabled:
        targets.append(ForwardTarget("superset", "svc/superset", 8088, 8088))
    if config.features.governance_enabled:
        targets.append(ForwardTarget("openmetadata", "svc/openmetadata", 8585, 8585, namespace=shared))

    banner = [f"Starting {backend.scope.upper()} POC port-forwards (Ctrl-C to stop all)..."]
    banner += [_BANNER_LINES[target.label] for target in targets]

    return ForwardSpec(
        targets=tuple(targets),
        namespace=config.namespace,
        context=kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        banner=tuple(banner),
    )
