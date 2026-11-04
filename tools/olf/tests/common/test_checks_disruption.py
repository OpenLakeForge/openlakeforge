from __future__ import annotations

from pathlib import Path

from olf.common.commands.checks import _disruption

_PDB = 'resource "kubernetes_pod_disruption_budget_v1" "x" {\n}\n'


def _module(tmp_path: Path, *files: str) -> list[str]:
    module = tmp_path / "infra/terraform/modules/demo"
    module.mkdir(parents=True)
    for i, text in enumerate(files):
        (module / f"f{i}.tf").write_text(text)
    return _disruption.disruption_errors(tmp_path)


def _values(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "infra/helm/values/local/x.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return _disruption.disruption_errors(tmp_path)


def test_single_replica_needs_no_budget(tmp_path: Path) -> None:
    assert _module(tmp_path, "spec {\n  replicas = 1\n}\n", "workers = 0\n") == []


def test_replicated_workload_without_budget_is_rejected(tmp_path: Path) -> None:
    errors = _module(tmp_path, "spec {\n  replicas = 3\n}\n")

    assert len(errors) == 1
    assert "f0.tf:2" in errors[0]


def test_expression_replica_count_is_held_to_the_rule(tmp_path: Path) -> None:
    assert len(_module(tmp_path, "workers = var.sizing.workers\n")) == 1


def test_budget_anywhere_in_the_module_satisfies_it(tmp_path: Path) -> None:
    assert _module(tmp_path, "replicas = 3\n", _PDB) == []


def test_values_with_several_replicas_need_a_budget(tmp_path: Path) -> None:
    assert len(_values(tmp_path, "server:\n  replicaCount: 2\n")) == 1


def test_values_with_a_budget_pass(tmp_path: Path) -> None:
    assert _values(tmp_path, "replicaCount: 2\npodDisruptionBudget:\n  enabled: true\n") == []


def test_values_with_one_replica_pass(tmp_path: Path) -> None:
    assert _values(tmp_path, "replicaCount: 1\nreplicas: 1\n") == []
