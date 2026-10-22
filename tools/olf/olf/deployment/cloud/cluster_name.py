"""The EKS cluster name every AWS command agrees on.

`olf deploy`, `olf destroy` and `olf e2e run` must name the same cluster: the
foundation passes it to Terraform, where a different name replaces the
cluster. Until 0.5 the default carried the `limited-` prefix the AWS test
account's IAM policy required (ADR 0010). A foundation created with that
default keeps it, because its Terraform state records the name.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

DEFAULT_AWS_CLUSTER_NAME = "eks-openlakeforge"


def aws_cluster_name(environ: Mapping[str, str], foundation_state_path: Path | None = None) -> str:
    """`AWS_CLUSTER_NAME`, else the name an existing foundation recorded, else the default."""
    explicit = environ.get("AWS_CLUSTER_NAME")
    if explicit:
        return explicit
    recorded = _recorded_cluster_name(foundation_state_path) if foundation_state_path else None
    return recorded or DEFAULT_AWS_CLUSTER_NAME


def _recorded_cluster_name(state_path: Path) -> str | None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    output = state.get("outputs", {}).get("cluster_name", {})
    if isinstance(output, dict) and isinstance(output.get("value"), str) and output["value"]:
        return str(output["value"])
    # A foundation apply that failed after creating the cluster but before
    # recording outputs still names it in its resources.
    for resource in state.get("resources", []):
        if resource.get("type") == "aws_eks_cluster":
            for instance in resource.get("instances", []):
                name = instance.get("attributes", {}).get("name")
                if isinstance(name, str) and name:
                    return name
    return None
