"""`olf check disruption`: a replicated workload carries a PodDisruptionBudget; single replicas carry none."""

from __future__ import annotations

import re
from pathlib import Path

import typer

from olf.common.commands.checks._shared import _root
from olf.common.log import fail

# ponytail: a line scan, as in _resources.py. It sees `replicas`, `replicaCount` and `workers` set to
# anything but a literal 0 or 1; a replica count hidden behind another name is not seen.
_TF_REPLICAS = re.compile(r"^\s*(?:replicas|replicaCount|workers)\s*=\s*(?!(?:0|1)\s*(?:#.*)?$)\S")
_VALUES_REPLICAS = re.compile(r"^\s*(?:replicas|replicaCount):\s*(?:[2-9]|\d{2,})\b")
_TF_PDB = 'resource "kubernetes_pod_disruption_budget_v1"'
_VALUES_PDB = re.compile(r"(?i)\b(?:poddisruptionbudget|pdb)\s*:")


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
    for file in sorted((root / "infra/helm/values").rglob("*.y*ml")):
        text = file.read_text(encoding="utf-8")
        if _VALUES_PDB.search(text):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if _VALUES_REPLICAS.match(line):
                where = file.relative_to(root).as_posix()
                errors.append(f"{where}:{number}: replicated workload, but the values enable no podDisruptionBudget")
    return errors


def disruption(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Require a PodDisruptionBudget wherever a workload runs more than one replica."""
    errors = disruption_errors(_root(repo_root))
    if errors:
        raise typer.Exit(code=fail("\n".join(errors)))
    typer.echo("Disruption budget checks passed.")
