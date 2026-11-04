"""`olf check disruption`: a replicated workload carries a PodDisruptionBudget; single replicas carry none."""

from __future__ import annotations

import re
from pathlib import Path

import typer
import yaml

from olf.common.commands.checks._shared import _root
from olf.common.log import fail

# ponytail: a line scan, as in _resources.py. It sees `replicas`, `replicaCount` and `workers` set to
# anything but a literal 0 or 1; a replica count hidden behind another name is not seen.
_TF_REPLICAS = re.compile(r"^\s*(?:replicas|replicaCount|workers)\s*=\s*(?!(?:0|1)\s*(?:#.*)?$)\S")
_VALUES_REPLICA_KEYS = frozenset({"replicas", "replicaCount", "workers"})
_TF_PDB = 'resource "kubernetes_pod_disruption_budget_v1"'
_VALUES_PDB_KEYS = frozenset({"poddisruptionbudget", "pdb"})


def _walk(node: object, path: str = "") -> list[tuple[str, object, object]]:
    """Every `(dotted key, key, value)` in a parsed values document."""
    found: list[tuple[str, object, object]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            dotted = f"{path}.{key}" if path else str(key)
            found.append((dotted, key, value))
            found += _walk(value, dotted)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _walk(item, f"{path}[{index}]")
    return found


def _enabled_budget(value: object) -> bool:
    """A budget block the chart renders: `enabled` not false, or a non-empty scalar setting."""
    if isinstance(value, dict):
        return value.get("enabled", True) is not False
    return bool(value)


def _values_errors(file: Path, where: str) -> list[str]:
    entries = [entry for document in yaml.safe_load_all(file.read_text(encoding="utf-8")) for entry in _walk(document)]
    if any(str(key).lower() in _VALUES_PDB_KEYS and _enabled_budget(value) for _, key, value in entries):
        return []
    return [
        f"{where}: {dotted}={value}: replicated workload, but the values enable no podDisruptionBudget"
        for dotted, key, value in entries
        if key in _VALUES_REPLICA_KEYS and isinstance(value, int) and not isinstance(value, bool) and value > 1
    ]


def disruption_errors(root: Path) -> list[str]:
    """Messages for replicated workloads without a PodDisruptionBudget.

    A single-replica pod is deliberately left without one: `minAvailable: 1` on a single pod blocks every
    node drain, and the v0.6 decision on #351 limits budgets to replicated services.
    """
    errors: list[str] = []
    for module in sorted({path.parent for path in (root / "infra/terraform/modules").rglob("*.tf")}):
        files = sorted(module.glob("*.tf"))
        if any(_TF_PDB in file.read_text(encoding="utf-8") for file in files):
            continue
        for file in files:
            for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if _TF_REPLICAS.match(line):
                    where = file.relative_to(root).as_posix()
                    errors.append(f"{where}:{number}: replicated workload, but {module.name} defines no {_TF_PDB}")
    # Values files are parsed, not scanned: a commented-out or `enabled: false` budget enables nothing.
    for file in sorted((root / "infra/helm/values").rglob("*.y*ml")):
        errors += _values_errors(file, file.relative_to(root).as_posix())
    return errors


def disruption(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Require a PodDisruptionBudget wherever a workload runs more than one replica."""
    errors = disruption_errors(_root(repo_root))
    if errors:
        raise typer.Exit(code=fail("\n".join(errors)))
    typer.echo("Disruption budget checks passed.")
