"""Trino/Polaris catalog, namespace, and table-count assertions."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from openlakeforge_domain import Domain, Product

from olf import log, trino
from olf.e2e._shell import E2EConfig, E2EError, kubectl

POLARIS_POD_SELECTOR = "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris"
POLARIS_RESTART_TIMEOUT_SECONDS = 300
# kindnet programs a new pod's policy asynchronously (ADR 0017 History), so a
# restarted Polaris is briefly unreachable after it reports Ready.
POLARIS_QUERY_RETRY_SECONDS = 60
PROBE_SCHEMA = "zz_olf_e2e_probe"
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


def list_tables(cfg: E2EConfig, schema: str, *, catalog: str | None = None) -> list[str]:
    """The tables Trino lists in `schema`. Table names are not tracked anywhere; the catalog is the source."""
    out = trino_query(cfg, f"SHOW TABLES IN {catalog or stage_catalog_name(cfg)}.{schema}")
    return [line.strip() for line in out.splitlines() if line.strip()]


def product_gold_tables(cfg: E2EConfig, product: Product) -> list[str]:
    """`<product>_gold.<table>` for each Gold table the product has, requiring at least one."""
    schema = f"{_glue_namespace_prefix(cfg)}{product.gold_namespace}"
    tables = list_tables(cfg, schema)
    if not tables:
        raise E2EError(f"{product.id}: Trino lists no tables in {stage_catalog_name(cfg)}.{schema}")
    return [f"{product.gold_namespace}.{table}" for table in tables]


def _check_silver_tables(cfg: E2EConfig, domain: Domain) -> None:
    """A domain with products has Silver tables, and each one is a Floe entity."""
    schema = f"{_glue_namespace_prefix(cfg)}{domain.silver_namespace}"
    tables = set(list_tables(cfg, schema))
    if domain.products and not tables:
        raise E2EError(f"{domain.name}: Trino lists no tables in {stage_catalog_name(cfg)}.{schema}")
    unknown = sorted(tables - {entity.name for entity in cfg.inventory.floe_entities(domain)})
    if unknown:
        raise E2EError(f"{schema} holds table(s) no Floe entity declares: {', '.join(unknown)}")


def _check_gold_rows(cfg: E2EConfig, product: Product) -> None:
    catalog = stage_catalog_name(cfg)
    prefix = _glue_namespace_prefix(cfg)
    for mart in product_gold_tables(cfg, product):
        count = trino_scalar(cfg, f"SELECT count(*) FROM {catalog}.{prefix}{mart}")
        if int(count) <= 0:
            raise E2EError(f"expected {catalog}.{prefix}{mart} to contain rows, got {count}")


def check_trino_tables_and_marts(cfg: E2EConfig) -> None:
    check_trino_catalog(cfg)
    log.step("Checking Silver and Gold tables...")
    for domain in cfg.inventory.domains:
        _check_silver_tables(cfg, domain)
    for product in cfg.inventory.products:
        _check_gold_rows(cfg, product)


def check_trino_product_tables_and_marts(cfg: E2EConfig, product: Product) -> None:
    """Verify the selected smoke product reached populated Silver and Gold tables."""
    check_trino_catalog(cfg)
    log.step(f"Checking Silver and Gold tables for {product.id}...")
    _check_silver_tables(cfg, cfg.inventory.domain_for_product(product))
    _check_gold_rows(cfg, product)


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
    """Append a row to a scratch Iceberg table through the long-lived Trino coordinator.

    No DROP: Polaris refuses the purge Trino's DROP TABLE requests, and the scratch schema
    keeps the table out of the Silver and Gold counts.
    """
    catalog = stage_catalog_name(cfg)
    schema = f"{catalog}.{_glue_namespace_prefix(cfg)}{PROBE_SCHEMA}"
    try:
        trino_query(cfg, f"CREATE SCHEMA IF NOT EXISTS {schema}")
        trino_query(cfg, f"CREATE TABLE IF NOT EXISTS {schema}.probe (probe integer)")
        trino_query(cfg, f"INSERT INTO {schema}.probe VALUES (1)")
    except E2EError as exc:
        # The CLI's own stderr does not survive `kubectl exec`; the coordinator log names the cause.
        args = ["logs", "-n", cfg.platform_namespace, "deploy/trino-coordinator", "--tail=200"]
        tail = kubectl(cfg, args, capture=True)
        raise E2EError(f"{exc}\nTrino coordinator log tail:\n{tail}") from exc


def trino_scalar(cfg: E2EConfig, sql: str) -> str:
    return parse_trino_scalar(trino_query(cfg, sql))


def trino_query(
    cfg: E2EConfig,
    sql: str,
    *,
    stage: str | None = None,
    password: str | None = None,
    session_user: str | None = None,
) -> str:
    """Run `sql` as a stage's runtime principal, with its password read from its Secret.

    The overrides are for the isolation probes: another stage's principal,
    a password that is not the principal's (`""` sends none), and a claimed
    session user apart from the authenticated principal.
    """
    catalog = stage_catalog_name(cfg)
    target = trino.TrinoTarget(
        provider=cfg.env,
        kube_context=cfg.kube_context,
        namespace=cfg.platform_namespace,
        catalog=catalog,
        stage=stage or trino.stage_from_catalog(catalog),
    )
    # Only a contract that carries the credential name is at the password level; the principal
    # Secrets exist at `off` and `tls` too, but nothing there asks for them.
    if not os.environ.get("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET"):
        password = None
    elif password is None:
        password = trino.decode_password(kubectl(cfg, trino.password_args(target), capture=True, retry_transient=True))
    return kubectl(
        cfg,
        trino.exec_args(target, sql, output_format="CSV_UNQUOTED", session_user=session_user),
        capture=True,
        retry_transient=True,
        input_text=None if password is None else f"{password}\n",
    )

def parse_trino_scalar(output: str) -> str:
    values = [line.strip().replace("\r", "") for line in output.splitlines() if line.strip()]
    if not values:
        raise E2EError("Trino query returned no scalar value.")
    return values[-1]


def assert_scalar_equals(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise E2EError(f"expected {label} {expected}, got {actual}")
