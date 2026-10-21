from pathlib import Path

import pytest
from conftest import E2E_INVENTORY, e2e_cfg

from olf import trino
from olf.e2e import _trino
from olf.e2e._shell import E2EError


def test_check_catalog_namespaces_reports_missing_descriptor_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    available = E2E_INVENTORY.silver_namespace_names | (
        E2E_INVENTORY.gold_namespace_names - {"order_revenue_gold"}
    )
    monkeypatch.setattr(_trino, "trino_query", lambda _cfg, _sql: "\n".join(sorted(available)))

    with pytest.raises(E2EError, match="order_revenue_gold"):
        _trino.check_catalog_namespaces(e2e_cfg(tmp_path))


def test_smoke_product_table_check_fails_for_an_empty_gold_mart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    product = E2E_INVENTORY.default_product
    silver_table_names = "\n".join(table.name for table in E2E_INVENTORY.resolved_silver_tables(product))
    values = iter((str(len(product.gold_tables)), "0"))
    monkeypatch.setattr(_trino, "check_trino_catalog", lambda _cfg: None)
    monkeypatch.setattr(_trino, "trino_query", lambda _cfg, _sql: silver_table_names)
    monkeypatch.setattr(_trino, "trino_scalar", lambda _cfg, _sql: next(values))

    with pytest.raises(E2EError, match="expected iceberg"):
        _trino.check_trino_product_tables_and_marts(e2e_cfg(tmp_path, suite="smoke"), product)


def test_parse_trino_scalar_returns_last_non_empty_line() -> None:
    assert _trino.parse_trino_scalar("\n15\r\n") == "15"


def test_trino_scalar_requests_transient_kubectl_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def kubectl(_cfg, args, *, capture=False, retry_transient=False, input_text=None):
        calls.append({"args": args, "capture": capture, "retry_transient": retry_transient})
        return "" if args[0] == "get" else "6\n"

    monkeypatch.setattr(_trino, "kubectl", kubectl)

    assert _trino.trino_scalar(e2e_cfg(tmp_path), "SELECT count(*) FROM iceberg.test.table") == "6"
    assert all(call["capture"] and call["retry_transient"] for call in calls)


def test_check_polaris_restart_recovery_waits_then_rechecks_trino(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    checks: list[str] = []

    def kubectl(_cfg, args, *, capture=False, retry_transient=False):
        commands.append(args)
        assert not retry_transient
        if args[0] == "get":
            assert capture
            return "polaris-6b8d7d9f5c-abcde"
        assert not capture
        return ""

    monkeypatch.setattr(_trino, "kubectl", kubectl)
    monkeypatch.setattr(_trino, "check_trino_write", lambda _cfg: checks.append("write"))
    monkeypatch.setattr(_trino, "check_trino_tables_and_marts", lambda _cfg: checks.append("trino"))

    _trino.check_polaris_restart_recovery(e2e_cfg(tmp_path))

    assert commands == [
        [
            "get",
            "pods",
            "-n",
            "lakehouse",
            "-l",
            "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        ["delete", "pod", "polaris-6b8d7d9f5c-abcde", "-n", "lakehouse"],
        [
            "wait",
            "--for=condition=Ready",
            "pod",
            "-n",
            "lakehouse",
            "-l",
            "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris",
            "--timeout=300s",
        ],
    ]
    assert checks == ["write", "trino"]


def test_check_polaris_restart_recovery_requires_a_pod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_trino, "kubectl", lambda *_args, **_kwargs: "")

    with pytest.raises(E2EError, match="could not find the Polaris pod"):
        _trino.check_polaris_restart_recovery(e2e_cfg(tmp_path))


def test_assert_scalar_equals_reports_mismatch() -> None:
    with pytest.raises(E2EError, match="expected Gold mart count 9, got 8"):
        _trino.assert_scalar_equals("8", "9", "Gold mart count")


def test_shared_services_are_queried_in_the_shared_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Trino and Polaris are deployed once per cluster, in a namespace of their
    own; only Dagster and Superset live in the stage namespace."""
    from dataclasses import replace

    commands: list[list[str]] = []

    def _fake_kubectl(_cfg, args, **_kwargs):  # noqa: ANN001, ANN202
        commands.append(args)
        return "" if args[0] == "get" else "trino-coordinator-1\n"

    monkeypatch.setattr(_trino, "kubectl", _fake_kubectl)
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-dev", shared_namespace="olf-system")

    _trino.trino_query(cfg, "SELECT 1")

    assert commands[-1][:4] == ["exec", "-i", "-n", "olf-system"]
    assert commands[0][:2] == ["get", "secret"]


@pytest.mark.parametrize("tls", [False, True])
def test_trino_query_uses_https_only_when_the_coordinator_serves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tls: bool
) -> None:
    """Run the command the coordinator pod would run, with a stub `trino`."""
    import subprocess

    stub = tmp_path / "trino"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    ca = tmp_path / "ca.crt"
    if tls:
        ca.write_text("pem")
    monkeypatch.setattr(trino, "TRINO_CA", str(ca))
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    def _run_in_pod(_cfg, args, **kwargs):  # noqa: ANN001, ANN202
        if args[0] != "exec":
            return ""
        return subprocess.run(
            args[args.index("--") + 1 :], capture_output=True, text=True, check=True, input=kwargs.get("input_text")
        ).stdout

    monkeypatch.setattr(_trino, "kubectl", _run_in_pod)

    argv = _trino.trino_query(e2e_cfg(tmp_path), "SELECT 1").splitlines()

    assert argv[-2:] == ["--execute", "SELECT 1"]
    assert ("--server" in argv) is tls
    if tls:
        assert argv[argv.index("--server") + 1] == "https://localhost:8443"
        assert argv[argv.index("--truststore-path") + 1] == str(ca)


def test_check_trino_write_appends_without_dropping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sql: list[str] = []
    monkeypatch.setattr(_trino, "trino_query", lambda _cfg, statement: sql.append(statement) or "")

    _trino.check_trino_write(e2e_cfg(tmp_path))

    assert [s.split(" ")[0] for s in sql] == ["CREATE", "CREATE", "INSERT"]
    assert all("zz_olf_e2e_probe" in s for s in sql)


def test_polaris_token_key_is_created_in_cluster_and_wired_by_name() -> None:
    """#412: a per-start key voids Trino's token on restart; #181: the key must stay out of Terraform state."""
    import hcl2

    text = (Path(__file__).parents[3] / "infra/terraform/modules/catalog/polaris/release.tf").read_text()
    assert "random_password\" \"token" not in text
    assert "symmetric.key=" in text and "/dev/urandom" in text  # generated by the Job
    assert 'resource "kubernetes_secret_v1" "token_broker"' not in text
    doc = hcl2.loads(text)
    resources = {(k, n) for r in doc["resource"] for k, blocks in r.items() for n in blocks}
    assert ("kubernetes_job_v1", "token_broker_key") in resources
    helm = next(b["polaris"] for r in doc["resource"] for k, b in r.items() if k == "helm_release")
    values = str(helm["values"])
    assert "tokenBroker" in values and "symmetric-key" in values and "local.token_broker_secret" in values
