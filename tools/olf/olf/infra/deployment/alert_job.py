"""A one-off Job made from the `alert-evaluator` CronJob's pod template, shared by the e2e and `olf infra alerts test`.

Both need the evaluator exactly as deployed -- image, Secret mounts, labels the NetworkPolicy admits, CA bundle -- plus
flags the CronJob itself never passes.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

EVALUATOR = "alert-evaluator"  # the CronJob's name (modules/observability/alerts)


def evaluator_job(
    cronjob: Mapping[str, Any],
    name: str,
    flags: Sequence[str],
    *,
    configmap: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The CronJob's pod template as a Job whose evaluator command gets `flags` appended.

    `configmap` is a `(name, mount path)` pair: that ConfigMap is mounted read-only at the path, e.g. settings the Job
    reads with `--settings`.
    """
    template = copy.deepcopy(cronjob["spec"]["jobTemplate"])
    pod = template["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if configmap:
        configmap_name, mount_path = configmap
        pod["volumes"] = [*pod.get("volumes", []), {"name": "e2e", "configMap": {"name": configmap_name}}]
        container["volumeMounts"] = [
            *container.get("volumeMounts", []),
            {"name": "e2e", "mountPath": mount_path, "readOnly": True},
        ]
    container["args"] = [*container.get("args", []), *flags]
    template["spec"].pop("ttlSecondsAfterFinished", None)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": cronjob["metadata"]["namespace"],
            "labels": template.get("metadata", {}).get("labels", {}),
        },
        "spec": template["spec"],
    }


def job_outcome(job: Mapping[str, Any]) -> str | None:
    status = job.get("status", {})
    if status.get("succeeded"):
        return "succeeded"
    if status.get("failed"):
        return "failed"
    return None
