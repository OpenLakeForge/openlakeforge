from __future__ import annotations

import subprocess
from pathlib import Path

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
    def runner(command, stdin):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    with pytest.raises(trino.TrinoQueryError, match=expected):
        trino.run_sql(_target(), "SELECT 1", output_format="ALIGNED", kubectl="kubectl", runner=runner)


def test_run_sql_returns_output_and_targets_the_kube_context() -> None:
    seen: list[list[str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="" if "get" in command else "42\n", stderr="")

    assert trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner) == "42\n"
    assert all(command[:3] == ["kubectl", "--context", "kind-openlakeforge-local"] for command in seen)


def test_run_sql_reads_no_secret_and_sends_no_stdin_without_a_named_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)
    calls: list[tuple[list[str], str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        calls.append((list(command), stdin))
        return subprocess.CompletedProcess(command, 0, stdout="1\n", stderr="")

    trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner)

    ((query, stdin),) = calls
    assert "get" not in query
    assert stdin == ""


def test_run_sql_sends_the_principals_password_on_stdin_never_in_the_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "trino-credential")

    calls: list[tuple[list[str], str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        calls.append((list(command), stdin))
        stdout = base64.b64encode(b"s3cret").decode() if "get" in command else "1\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner)

    (lookup, _), (query, stdin) = calls
    assert "trino-credential-olf-prod-runtime" in lookup
    assert stdin == "s3cret\n"
    assert not any("s3cret" in part for part in query)


def test_the_pod_script_reads_the_password_only_when_the_pod_enforces_one(tmp_path: Path) -> None:
    import subprocess as sp

    (tmp_path / "trino").write_text('#!/bin/sh\nprintf "%s|%s\\n" "$TRINO_PASSWORD" "$*"\n')
    (tmp_path / "trino").chmod(0o755)
    password_file = tmp_path / "password.db"

    def run(stdin: str) -> str:
        script = trino.cli_script(str(tmp_path / "absent-ca")).replace(trino.TRINO_PASSWORD_FILE, str(password_file))
        return sp.run(
            ["sh", "-c", script, "trino", "--user", "u"],
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        ).stdout.strip()

    assert run("ignored\n") == "|--user u"
    password_file.write_text("u hash")
    assert run("pw\n") == "pw|--password --user u"
    assert run("\n") == "|--user u"


def test_run_sql_stops_when_the_password_secret_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "trino-credential")
    calls: list[list[str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="forbidden")

    with pytest.raises(trino.TrinoQueryError, match=r"(?s)trino-credential-olf-prod-runtime.*RBAC"):
        trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner)
    assert len(calls) == 1
