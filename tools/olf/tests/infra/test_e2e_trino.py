from pathlib import Path

import pytest
from conftest import E2E_INVENTORY, e2e_cfg

from olf.infra import trino
from olf.infra.e2e import _trino
from olf.infra.e2e._shell import E2EError


@pytest.fixture(autouse=True)
def _kubectl_on_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # `trino_query` resolves the binary before `run_sql` is reached; none is installed in CI.
    monkeypatch.setattr(_trino, "_kubectl_executable", lambda *_args: "kubectl")


def test_check_catalog_namespaces_reports_missing_descriptor_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    available = E2E_INVENTORY.silver_namespace_names | (
        E2E_INVENTORY.gold_namespace_names - {"order_revenue_gold"}
    )
    monkeypatch.setattr(_trino, "trino_query", lambda _cfg, _sql: "\n".join(sorted(available)))

    with pytest.raises(E2EError, match="order_revenue_gold"):
        _trino.check_catalog_namespaces(e2e_cfg(tmp_path))


def _trino_listing(monkeypatch: pytest.MonkeyPatch, tables: dict[str, list[str]]) -> None:
    """Answer `SHOW TABLES IN <catalog>.<schema>` from `tables`, keyed by schema."""
    monkeypatch.setattr(_trino, "check_trino_catalog", lambda _cfg: None)
    monkeypatch.setattr(
        _trino, "trino_query", lambda _cfg, sql: "\n".join(tables.get(sql.rsplit(".", 1)[-1], []))
    )


def test_smoke_product_table_check_fails_for_an_empty_gold_mart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    product = E2E_INVENTORY.default_product
    _trino_listing(monkeypatch, {product.silver_namespace: ["orders"], product.gold_namespace: ["mart"]})
    monkeypatch.setattr(_trino, "trino_scalar", lambda _cfg, _sql: "0")

    with pytest.raises(E2EError, match="expected iceberg"):
        _trino.check_trino_product_tables_and_marts(e2e_cfg(tmp_path, suite="smoke"), product)


def test_product_table_check_requires_a_gold_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    product = E2E_INVENTORY.default_product
    _trino_listing(monkeypatch, {product.silver_namespace: ["orders"]})

    with pytest.raises(E2EError, match=f"{product.id}: Trino lists no tables"):
        _trino.check_trino_product_tables_and_marts(e2e_cfg(tmp_path, suite="smoke"), product)


def test_product_table_check_rejects_a_silver_table_no_floe_entity_declares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    product = E2E_INVENTORY.default_product
    _trino_listing(monkeypatch, {product.silver_namespace: ["orders", "stray"], product.gold_namespace: ["mart"]})

    with pytest.raises(E2EError, match="no Floe entity declares: stray"):
        _trino.check_trino_product_tables_and_marts(e2e_cfg(tmp_path, suite="smoke"), product)


def test_parse_trino_scalar_returns_last_non_empty_line() -> None:
    assert _trino.parse_trino_scalar("\n15\r\n") == "15"


def test_a_transient_failure_reaching_trino_is_retried_but_a_query_failure_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = iter(
        [trino.TrinoQueryError("cannot reach Trino at 127.0.0.1: ConnectionError"), "6\n"],
    )
    attempts: list[str] = []

    def run_sql(_target, sql, **_kwargs):  # noqa: ANN001, ANN202
        attempts.append(sql)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(trino, "run_sql", run_sql)
    monkeypatch.setattr(_trino.time, "sleep", lambda _seconds: None)

    assert _trino.trino_scalar(e2e_cfg(tmp_path), "SELECT count(*) FROM iceberg.test.table") == "6"
    assert len(attempts) == 2

    attempts.clear()

    def denied(*_args: object, **_kwargs: object) -> str:
        raise trino.TrinoQueryError("Access Denied")

    monkeypatch.setattr(trino, "run_sql", denied)
    with pytest.raises(E2EError, match="Access Denied"):
        _trino.trino_query(e2e_cfg(tmp_path), "SELECT 1")


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

    targets: list[trino.TrinoTarget] = []

    def run_sql(target, _sql, **_kwargs):  # noqa: ANN001, ANN202
        targets.append(target)
        return "1\n"

    monkeypatch.setattr(trino, "run_sql", run_sql)
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-dev", shared_namespace="olf-system")

    _trino.trino_query(cfg, "SELECT 1")

    assert targets[0].namespace == "olf-system"


@pytest.mark.parametrize(
    ("credential", "password", "expected"),
    [
        ("trino-credential", None, None),
        ("trino-credential", "", ""),
        ("trino-credential", "wrong", "wrong"),
        ("", "wrong", None),
    ],
    ids=("secret", "none-sent", "override", "no-credential-in-contract"),
)
def test_the_password_follows_the_contract_and_the_probe_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential: str, password: str | None, expected: str | None
) -> None:
    """None lets `run_sql` read the principal's Secret; a contract without a credential never sends one."""
    seen: list[dict] = []
    monkeypatch.setattr(trino, "run_sql", lambda _t, _s, **kwargs: seen.append(kwargs) or "1\n")
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", credential)
    cfg = e2e_cfg(tmp_path)

    _trino.trino_query(cfg, "SELECT 1", password=password, session_user="olf-prod-runtime")

    assert seen[0]["password"] == expected
    assert seen[0]["session_user"] == "olf-prod-runtime"
    assert seen[0]["output_format"] == "bare"


def test_check_trino_write_appends_and_drops_a_dbt_temporary_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#741: an incremental model's second run drops `<model>__dbt_tmp`; the probe makes the same drop."""
    sql: list[str] = []
    monkeypatch.setattr(_trino, "trino_query", lambda _cfg, statement: sql.append(statement) or "")

    _trino.check_trino_write(e2e_cfg(tmp_path))

    assert [s.split(" ")[0] for s in sql] == ["CREATE", "CREATE", "INSERT", "CREATE", "DROP"]
    assert all("zz_olf_e2e_probe" in s for s in sql)
    assert sql[-1].startswith("DROP VIEW ") and sql[-1].endswith("__dbt_tmp")


def test_polaris_token_key_is_created_in_cluster_and_wired_by_name() -> None:
    """#412: a per-start key voids Trino's token on restart; #181: the key must stay out of Terraform state."""
    import hcl2

    text = (Path(__file__).parents[4] / "infra/terraform/modules/catalog/polaris/release.tf").read_text()
    assert "random_password\" \"token" not in text
    assert "symmetric.key=" in text and "/dev/urandom" in text  # generated by the Job
    assert 'resource "kubernetes_secret_v1" "token_broker"' not in text
    doc = hcl2.loads(text)
    resources = {(k, n) for r in doc["resource"] for k, blocks in r.items() for n in blocks}
    assert ("kubernetes_job_v1", "token_broker_key") in resources
    helm = next(b["polaris"] for r in doc["resource"] for k, b in r.items() if k == "helm_release")
    values = str(helm["values"])
    assert "tokenBroker" in values and "symmetric-key" in values and "local.token_broker_secret" in values
