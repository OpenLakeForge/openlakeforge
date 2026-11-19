"""Explaining an unreachable local kind context, shared by deploy and teardown."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml

from olf.common.deployment.errors import ExecutableNotFoundError
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.distribution import default_home
from olf.common.tooling.toolkit import Toolkit

SOURCE_OLF = "uv run --project tools/olf --locked olf"
INSTALLED_OLF = "olf"

Action = Literal["deploy", "destroy"]

_CONSEQUENCE: dict[Action, str] = {
    "deploy": "Run the foundation phase before applying the local platform.",
    "destroy": "The platform must be destroyed before the local foundation is destroyed.",
}
_REMEDY: dict[Action, str] = {"deploy": "deploy", "destroy": "tear it down"}


def unreachable_context_message(
    config: LocalDeploymentConfig, tools: Toolkit, *, action: Action, env: Mapping[str, str]
) -> str:
    """Why `olf infra <action>` cannot reach the cluster, naming the state it read.

    The source-tree and installed `olf` keep separate state roots for the same
    kind cluster (#1282), so a run in the mode that did not deploy reads the
    other mode's stale kubeconfig. This does not refresh that kubeconfig from
    kind: the Terraform state beside it belongs to the earlier deployment too, and
    applying or destroying with it would act on the wrong state. It points at the
    root whose kubeconfig matches the running cluster instead.
    """
    paths = config.paths
    kubeconfig = paths.kubeconfig_path
    if paths.installed:
        mode, state_root = "installed olf", paths.state_root
        other_mode, other_kubeconfig, other_command = (
            "source-tree olf",
            paths.repo_root / ".tmp" / "kubeconfigs" / "local.yaml",
            f"{SOURCE_OLF} infra {action} (from the repository root)",
        )
    else:
        mode, state_root = "source-tree olf", paths.work_root
        other_mode, other_kubeconfig, other_command = (
            "installed olf",
            default_home() / "state" / "local" / "kubeconfig.yaml",
            f"{INSTALLED_OLF} infra {action} (the installed olf, from the project directory)",
        )
    lines = [
        f"Kubernetes context '{config.kube_context}' is not reachable through kubeconfig {kubeconfig} "
        f"({mode} state root {state_root}; foundation state {paths.foundation_state_path}). " + _CONSEQUENCE[action]
    ]

    cluster = config.cluster.name
    try:
        live = tools.kind.get_kubeconfig(cluster, env=env)
    except ExecutableNotFoundError:
        return lines[0]
    if not live.ok:
        lines.append(f"No kind cluster named '{cluster}' is running.")
        return "\n".join(lines)

    live_server = _server(_load(live.stdout), config.kube_context)
    if live_server is None:
        return lines[0]
    read_server = _server(_read(kubeconfig), config.kube_context)
    if read_server != live_server:
        found = f"points at {read_server}" if read_server else "has no server for that context"
        lines.append(
            f"kind cluster '{cluster}' is running at {live_server}, but {kubeconfig} {found}: "
            f"this state belongs to another deployment of '{cluster}'."
        )
    if other_kubeconfig != kubeconfig and _server(_read(other_kubeconfig), config.kube_context) == live_server:
        lines.append(
            f"The {other_mode} kubeconfig {other_kubeconfig} matches the running cluster; "
            f"{_REMEDY[action]} with the mode that deployed it: {other_command}."
        )
    return "\n".join(lines)


def _read(path: Path) -> Any:
    try:
        return _load(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _load(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _server(document: Any, context_name: str) -> str | None:
    if not isinstance(document, dict):
        return None
    cluster_name = next(
        (
            entry.get("context", {}).get("cluster")
            for entry in document.get("contexts") or ()
            if isinstance(entry, dict) and entry.get("name") == context_name and isinstance(entry.get("context"), dict)
        ),
        None,
    )
    return next(
        (
            entry["cluster"].get("server")
            for entry in document.get("clusters") or ()
            if isinstance(entry, dict) and entry.get("name") == cluster_name and isinstance(entry.get("cluster"), dict)
        ),
        None,
    )
