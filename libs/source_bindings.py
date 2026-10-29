"""The container env a product job's run pod reads its sources' stage bindings from."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from openlakeforge_domain.inventory import Source

# dagster-k8s appends this container env to the run pod the run launcher
# creates for a job, after the code location's own env.
K8S_CONFIG_TAG = "dagster-k8s/config"


def source_binding_env(sources: Iterable[Source]) -> list[dict[str, Any]]:
    """Kubernetes env entries for every runtime key the sources declare.

    Each entry is a ConfigMap or Secret key reference, never a value: the
    stage's namespace holds the binding, so the same image and revision read
    DEV's endpoint in DEV and PROD's in PROD (#546).
    """
    env: list[dict[str, Any]] = []
    for source in sources:
        for key in source.runtime_config:
            env.append(
                {
                    "name": source.runtime_env_name(key),
                    "valueFrom": {"configMapKeyRef": {"name": source.config_map_name, "key": key}},
                }
            )
        for key in source.runtime_secrets:
            env.append(
                {
                    "name": source.runtime_env_name(key),
                    "valueFrom": {"secretKeyRef": {"name": source.credentials_secret_name, "key": key}},
                }
            )
    return env


def source_binding_tags(sources: Iterable[Source]) -> dict[str, str]:
    """The job tag that puts the sources' bindings on the job's run pod only.

    The code-location server never receives them. Steps of one run share its
    pod, though, so every loader in a job sees every source binding the job
    carries; per-step isolation would need a pod per step.
    """
    env = source_binding_env(sources)
    if not env:
        return {}
    return {K8S_CONFIG_TAG: json.dumps({"container_config": {"env": env}}, sort_keys=True)}
