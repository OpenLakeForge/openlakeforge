"""Trino/Polaris catalog, namespace, and table-count assertions."""

from __future__ import annotations

import time
from collections.abc import Callable

from openlakeforge_domain import Product

from olf import log, trino
from olf.e2e._shell import E2EConfig, E2EError, kubectl

POLARIS_POD_SELECTOR = "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris"
POLARIS_RESTART_TIMEOUT_SECONDS = 300
# kindnet programs a new pod's policy asynchronously (ADR 0017 History), so a
# restarted Polaris is briefly unreachable after it reports Ready.
POLARIS_QUERY_RETRY_SECONDS = 60
POLARIS_QUERY_RETRY_INTERVAL_SECONDS = 5

def stage_catalog_name(cfg: E2EConfig) -> str:
    """The Iceberg catalog this run's stage is served under (see `olf.trino.stage_catalog_name`).

    Takes `cfg` for call-site symmetry with the rest of this module.
    """
    del cfg
    return trino.stage_catalog_name()


def _glue_namespace_prefix(cfg: E2EConfig) -> str:
    return trino.glue_namespace_prefix(cfg.env, stage_catalog_name(cfg))

def check_trino_catalog(cfg: E2EConfig) -> None:
    log.step("Checking Trino catalogs...")
    catalog = stage_catalog_name(cfg)
    catalogs = trino_query(cfg, "SHOW CATALOGS")
    if catalog not in set(catalogs.splitlines()):
        raise E2EError(f"Trino did not expose the {catalog} catalog.")


def check_catalog_namespaces(cfg: E2EConfig) -> None:
    """Verify Polaris exposes every descriptor-derived namespace through Trino."""
    log.step("Checking Polaris namespaces through Trino...")
    catalog = stage_catalog_name(cfg)
    prefix = _glue_namespace_prefix(cfg)
    namespaces = set(trino_query(cfg, f"SHOW SCHEMAS FROM {catalog}").splitlines())
    expected = {
        f"{prefix}{name}"
        for name in (
            cfg.inventory.bronze_namespace_names
            | cfg.inventory.silver_namespace_names
            | cfg.inventory.gold_namespace_names
        )
    }
    missing = sorted(expected - namespaces)
    if missing:
        raise E2EError("Polaris is missing descriptor-derived namespaces: " + ", ".join(missing))


def _schema_in_list(schema_names: set[str]) -> str:
    return ", ".join(f"'{name}'" for name in sorted(schema_names))


def check_trino_tables_and_marts(cfg: E2EConfig) -> None:
    check_trino_catalog(cfg)
    log.step("Checking Silver and Gold table counts...")
    catalog = stage_catalog_name(cfg)
    prefix = _glue_namespace_prefix(cfg)
    silver_count = trino_scalar(
        cfg,
        f"SELECT count(*) FROM {catalog}.information_schema.tables "
        f"WHERE table_schema IN ({_schema_in_list({f'{prefix}{n}' for n in cfg.inventory.silver_namespace_names})})",
    )
    gold_count = trino_scalar(
        cfg,
        f"SELECT count(*) FROM {catalog}.information_schema.tables "
        f"WHERE table_schema IN ({_schema_in_list({f'{prefix}{n}' for n in cfg.inventory.gold_namespace_names})})",
    )
    assert_scalar_equals(silver_count, str(cfg.inventory.silver_table_count), "Silver table count")
    assert_scalar_equals(gold_count, str(cfg.inventory.gold_table_count), "Gold mart count")

    for mart in cfg.inventory.gold_mart_names:
        mart = f"{prefix}{mart}"
        count = trino_scalar(cfg, f"SELECT count(*) FROM {catalog}.{mart}")
        if int(count) <= 0:
            raise E2EError(f"expected {catalog}.{mart} to contain rows, got {count}")


def check_trino_product_tables_and_marts(cfg: E2EConfig, product: Product) -> None:
    """Verify the selected smoke product reached populated Silver and Gold tables.

    Silver is checked by table name, not by namespace row count: two products
    in the same domain share one Silver namespace, so another product's
    tables (or a sibling's earlier smoke run) can already be present there.
    Gold stays product-exclusive, so a namespace-wide count is still valid.
    """
    check_trino_catalog(cfg)
    catalog = stage_catalog_name(cfg)
    prefix = _glue_namespace_prefix(cfg)
    silver_namespace = f"{prefix}{product.silver_namespace}"
    gold_namespace = f"{prefix}{product.gold_namespace}"
    log.step(f"Checking Silver and Gold tables for {product.id}...")
    silver_tables = set(
        trino_query(
            cfg,
            f"SELECT table_name FROM {catalog}.information_schema.tables "
            f"WHERE table_schema = '{silver_namespace}'",
        ).splitlines()
    )
    missing_silver = sorted(
        table.name for table in cfg.inventory.resolved_silver_tables(product) if table.name not in silver_tables
    )
    if missing_silver:
        raise E2EError(f"{product.id}: missing Silver table(s) in {silver_namespace}: {', '.join(missing_silver)}")
    gold_count = trino_scalar(
        cfg,
        f"SELECT count(*) FROM {catalog}.information_schema.tables WHERE table_schema = '{gold_namespace}'",
    )
    assert_scalar_equals(gold_count, str(len(product.gold_tables)), f"{product.id} Gold mart count")
    for mart in product.gold_mart_names:
        mart = f"{prefix}{mart}"
        count = trino_scalar(cfg, f"SELECT count(*) FROM {catalog}.{mart}")
        if int(count) <= 0:
            raise E2EError(f"expected {catalog}.{mart} to contain rows, got {count}")


def retry_until_deadline(
    action: Callable[[], None],
    *,
    timeout: float = POLARIS_QUERY_RETRY_SECONDS,
    interval: float = POLARIS_QUERY_RETRY_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run `action` until it succeeds; re-raise its last E2EError once `timeout` has passed."""
    deadline = clock() + timeout
    while True:
        try:
            action()
            return
        except E2EError:
            if clock() >= deadline:
                raise
            sleep(interval)


def check_polaris_restart_recovery(cfg: E2EConfig) -> None:
    """Verify that Polaris keeps table identity after its pod is recreated."""
    log.step("Checking Polaris restart recovery...")
    pod_name = kubectl(
        cfg,
        [
            "get",
            "pods",
            "-n",
            cfg.platform_namespace,
            "-l",
            POLARIS_POD_SELECTOR,
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture=True,
    ).strip()
    if not pod_name:
        raise E2EError("could not find the Polaris pod to restart.")

    kubectl(cfg, ["delete", "pod", pod_name, "-n", cfg.platform_namespace])
    kubectl(
        cfg,
        [
            "wait",
            "--for=condition=Ready",
            "pod",
            "-n",
            cfg.platform_namespace,
            "-l",
            POLARIS_POD_SELECTOR,
            f"--timeout={POLARIS_RESTART_TIMEOUT_SECONDS}s",
        ],
    )
    # Write first: a read-only recheck passed while commits failed with "Not
    # authorized" (#412) because Polaris signed tokens with a per-start key.
    retry_until_deadline(lambda: check_trino_write(cfg))
    retry_until_deadline(lambda: check_trino_tables_and_marts(cfg))


def check_trino_write(cfg: E2EConfig) -> None:
    """Create and drop a scratch Iceberg table through the long-lived Trino coordinator."""
    catalog = stage_catalog_name(cfg)
    schema = f"{_glue_namespace_prefix(cfg)}{sorted(cfg.inventory.gold_namespace_names)[0]}"
    table = f"{catalog}.{schema}.olf_restart_probe"
    trino_query(cfg, f"DROP TABLE IF EXISTS {table}")
    trino_query(cfg, f"CREATE TABLE {table} AS SELECT 1 AS probe")
    trino_query(cfg, f"DROP TABLE {table}")


def trino_scalar(cfg: E2EConfig, sql: str) -> str:
    return parse_trino_scalar(trino_query(cfg, sql))


def trino_query(cfg: E2EConfig, sql: str) -> str:
    catalog = stage_catalog_name(cfg)
    target = trino.TrinoTarget(
        provider=cfg.env,
        kube_context=cfg.kube_context,
        namespace=cfg.platform_namespace,
        catalog=catalog,
        stage=trino.stage_from_catalog(catalog),
    )
    return kubectl(
        cfg,
        trino.exec_args(target, sql, output_format="CSV_UNQUOTED"),
        capture=True,
        retry_transient=True,
    )

def parse_trino_scalar(output: str) -> str:
    values = [line.strip().replace("\r", "") for line in output.splitlines() if line.strip()]
    if not values:
        raise E2EError("Trino query returned no scalar value.")
    return values[-1]


def assert_scalar_equals(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise E2EError(f"expected {label} {expected}, got {actual}")
