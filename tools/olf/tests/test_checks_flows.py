from __future__ import annotations

from pathlib import Path

from olf.common.commands.checks._flows import ADR_PATH, FLOWS_PATH, flow_errors

ROOT = Path(__file__).resolve().parents[3]
VALID = (
    "workloads:\n  pg: {app.kubernetes.io/name: postgresql}\nflows:\n  - id: F6\n    from: {scope: stage}\n"
    "    to: {scope: shared, pods: {app.kubernetes.io/name: postgresql}}\n    ports: [5432]\n    tag: V\n"
)


def _errors(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "flows.yaml"
    path.write_text(text)
    return flow_errors(path, ROOT / ADR_PATH)


def test_repository_flow_file_is_valid() -> None:
    assert flow_errors(ROOT / FLOWS_PATH, ROOT / ADR_PATH) == []


def test_valid_row_passes(tmp_path: Path) -> None:
    assert _errors(tmp_path, VALID) == []


def test_rejects_unknown_flow_id_port_scope_and_tag(tmp_path: Path) -> None:
    errors = _errors(
        tmp_path,
        VALID.replace("F6", "F99").replace("5432", "70000").replace("shared", "elsewhere").replace("tag: V", "tag: X"),
    )

    joined = "\n".join(errors)
    assert "ADR 0017" in joined and "ports" in joined and "scope" in joined and "tag" in joined


def test_rejects_pods_on_a_stage_endpoint(tmp_path: Path) -> None:
    assert _errors(tmp_path, VALID.replace("from: {scope: stage}", "from: {scope: stage, pods: {a: b}}"))


def test_rejects_missing_or_empty_workloads(tmp_path: Path) -> None:
    assert _errors(tmp_path, VALID.replace("  pg: {app.kubernetes.io/name: postgresql}\n", ""))


def test_source_list_is_checked_per_entry(tmp_path: Path) -> None:
    listed = VALID.replace("from: {scope: stage}", "from: [{scope: stage}, {scope: node}]")
    assert _errors(tmp_path, listed) == []
    assert _errors(tmp_path, listed.replace("{scope: node}", "{scope: any, pods: {a: b}}"))
