"""`olf check flows`: release/network-flows.yaml against ADR 0017's flow table."""

from __future__ import annotations

import re
from pathlib import Path

import typer
import yaml

from olf.commands._shared import fail
from olf.commands.checks._shared import _root

FLOWS_PATH = "release/network-flows.yaml"
ADR_PATH = "docs/adr/0017-local-network-policy-and-rbac.md"
SCOPES = {"stage", "shared", "kube-system", "apiserver"}
PROTOCOLS = {"TCP", "UDP"}


def _endpoint_errors(label: str, endpoint: object) -> list[str]:
    if not isinstance(endpoint, dict) or endpoint.get("scope") not in SCOPES:
        return [f"{label}: scope must be one of {sorted(SCOPES)}"]
    pods = endpoint.get("pods", {})
    if not isinstance(pods, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in pods.items()):
        return [f"{label}: pods must map label names to string values"]
    if pods and endpoint["scope"] in {"stage", "apiserver"}:
        return [f"{label}: pods cannot narrow scope {endpoint['scope']}"]
    return []


def flow_errors(flows_file: Path, adr_file: Path) -> list[str]:
    """Every problem in the flow file; an empty list means it is valid."""
    document = yaml.safe_load(flows_file.read_text()) or {}
    flows = document.get("flows")
    if not isinstance(flows, list) or not flows:
        return [f"{flows_file.name}: flows must be a non-empty list"]
    workloads = document.get("workloads")
    if not isinstance(workloads, dict) or not workloads or not all(
        isinstance(labels, dict) and labels and all(isinstance(v, str) for v in labels.values())
        for labels in workloads.values()
    ):
        return [f"{flows_file.name}: workloads must map names to non-empty label maps"]
    adr_ids = set(re.findall(r"^\| (F\d+) \|", adr_file.read_text(), re.MULTILINE))
    errors: list[str] = []
    for index, row in enumerate(flows):
        if not isinstance(row, dict):
            errors.append(f"flows[{index}]: must be a mapping")
            continue
        label = f"flows[{index}] {row.get('id')}"
        if row.get("id") not in adr_ids:
            errors.append(f"{label}: id is not a row of the ADR 0017 flow table")
        errors += _endpoint_errors(f"{label} from", row.get("from"))
        errors += _endpoint_errors(f"{label} to", row.get("to"))
        ports = row.get("ports")
        if not isinstance(ports, list) or not ports or not all(
            isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in ports
        ):
            errors.append(f"{label}: ports must be a non-empty list of port numbers")
        if not set(row.get("protocols", ["TCP"])) <= PROTOCOLS:
            errors.append(f"{label}: protocols must be a subset of {sorted(PROTOCOLS)}")
        if row.get("tag") not in {"V", "T"}:
            errors.append(f"{label}: tag must be V or T")
    return errors


def flows(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Validate the NetworkPolicy flow file."""
    root = _root(repo_root)
    errors = flow_errors(root / FLOWS_PATH, root / ADR_PATH)
    for error in errors:
        typer.echo(error)
    if errors:
        raise typer.Exit(code=fail(f"{FLOWS_PATH} is invalid"))
    typer.echo(f"{FLOWS_PATH}: ok")
