from __future__ import annotations

import subprocess

import pytest

from olf import trino


def _target(**overrides: str) -> trino.TrinoTarget:
    fields = {
        "provider": "local",
        "kube_context": "kind-openlakeforge-local",
        "namespace": "olf-system",
        "catalog": "lakehouse_prod",
        "stage": "prod",
    }
    return trino.TrinoTarget(**{**fields, **overrides})


def test_stage_catalog_comes_from_the_applied_contract_and_defaults_to_iceberg() -> None:
    assert trino.stage_catalog_name({"OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_uat"}) == "lakehouse_uat"
    assert trino.stage_catalog_name({}) == "iceberg"


def test_runtime_user_follows_the_stage_not_the_namespace() -> None:
    assert _target(namespace="lakehouse").user == "olf-prod-runtime"


def test_only_aws_prefixes_glue_namespaces() -> None:
    assert trino.glue_namespace_prefix("aws", "iceberg") == "iceberg_"
    assert trino.glue_namespace_prefix("local", "lakehouse_dev") == ""


def test_exec_args_set_the_stage_catalog_as_the_session_default() -> None:
    args = trino.exec_args(_target(), "SELECT 1", output_format="ALIGNED")

    assert args[args.index("--catalog") + 1] == "lakehouse_prod"
    assert args[args.index("--user") + 1] == "olf-prod-runtime"
    assert args[args.index("-n") + 1] == "olf-system"
    assert args[-2:] == ["--execute", "SELECT 1"]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Query failed: Access Denied: Cannot select from table x", "denied the query for olf-prod-runtime"),
        ("Query failed: Catalog 'lakehouse_prod' does not exist", "does not look deployed"),
        ('Error from server (NotFound): deployments.apps "trino-coordinator" not found', "does not look deployed"),
        ("Query failed: line 1:8: Column 'x' cannot be resolved", "Column 'x' cannot be resolved"),
    ],
)
def test_run_sql_explains_failures(stderr: str, expected: str) -> None:
    def runner(command):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    with pytest.raises(trino.TrinoQueryError, match=expected):
        trino.run_sql(_target(), "SELECT 1", output_format="ALIGNED", kubectl="kubectl", runner=runner)


def test_run_sql_returns_output_and_targets_the_kube_context() -> None:
    seen: list[list[str]] = []

    def runner(command):  # noqa: ANN001, ANN202
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="42\n", stderr="")

    assert trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner) == "42\n"
    assert seen[0][:3] == ["kubectl", "--context", "kind-openlakeforge-local"]
