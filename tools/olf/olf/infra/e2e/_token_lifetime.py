"""A Gold build that outlasts the Polaris token (#1061).

A dbt process holds the Polaris token it started with and Trino hands it to Polaris at every commit, so a
build longer than the token's lifetime failed at its first commit after expiry. The proof cannot wait the
default hour: it runs the product's Gold models once to time them, shortens Polaris's token lifetime to a
fraction of that time, and runs them again. The second run must pass and must have lasted longer than the
lifetime, or it proved nothing.

The lifetime is set on the running Polaris Deployment for the length of the check and removed afterwards.
It is not a profile field and no Terraform input: a stack must never be configured with tokens this short.
"""

from __future__ import annotations

import re
from graphlib import TopologicalSorter
from pathlib import Path

from olf.common import log
from olf.infra.e2e._dagster import dagster_client, launch_and_poll
from olf.infra.e2e._maintenance import base_tables
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl
from olf.infra.e2e._trino import (
    POLARIS_POD_SELECTOR,
    POLARIS_RESTART_TIMEOUT_SECONDS,
    _glue_namespace_prefix,
    check_trino_write,
    retry_until_deadline,
)

# Quarkus maps `polaris.authentication.token-broker.max-token-generation` (Polaris 1.8, default PT1H) to this.
LIFETIME_ENV = "POLARIS_AUTHENTICATION_TOKEN_BROKER_MAX_TOKEN_GENERATION"
# The second run lasts about as long as the first, so a lifetime below it is outlasted. The runtime builds one
# dbt generation (models whose upstream models are all built) per token, so each generation's own dbt process
# (start-up and build) must fit inside the lifetime. A floor and 70% of the whole build leave room for that
# when a product has two or more generations of similar length; a product with a single generation cannot
# pass, because its one process is the whole build, so the check skips it.
LIFETIME_FRACTION = 0.7
MIN_LIFETIME_SECONDS = 15
_REF = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""")


def shortened_lifetime_seconds(baseline_seconds: float) -> int:
    return max(MIN_LIFETIME_SECONDS, int(baseline_seconds * LIFETIME_FRACTION))


def generation_count(models_dir: Path) -> int:
    """How many topological generations the `ref()` graph of the `.sql` models under `models_dir` has."""
    sources = {path.stem: path.read_text() for path in models_dir.rglob("*.sql")}
    graph = {name: {ref for ref in _REF.findall(sql) if ref in sources} for name, sql in sources.items()}
    sorter = TopologicalSorter(graph)
    sorter.prepare()
    generations = 0
    while sorter.is_active():
        sorter.done(*sorter.get_ready())
        generations += 1
    return generations


def check_gold_outlasts_polaris_token(cfg: E2EConfig) -> None:
    product = cfg.inventory.default_product
    generations = generation_count(cfg.repo_root / product.dbt_project_dir / "models")
    if generations < 2:
        log.info(
            f"Skipping the token lifetime proof: {product.id}'s Gold builds in {generations} dbt generation(s), "
            "one process for the whole build, so no shortened lifetime can be outlasted by it. It needs two."
        )
        return
    tables = base_tables(cfg, f"{_glue_namespace_prefix(cfg)}{product.gold_namespace}")
    if len(tables) < 2:
        log.info(f"Skipping the token lifetime proof: {product.id} has {len(tables)} Gold table(s), it needs two.")
        return
    selection = [(product.id, table) for table in tables]
    log.step(f"Building {product.id}'s Gold models past a shortened Polaris token lifetime...")
    with dagster_client(cfg) as client:

        def gold_seconds() -> float:
            run = client.get_run(launch_and_poll(cfg, client, product.job_name, asset_selection=selection))
            if run is None or run.started_at is None or run.ended_at is None:
                raise E2EError(f"Dagster recorded no start and end time for the {product.id} Gold run.")
            return run.ended_at - run.started_at

        baseline = gold_seconds()
        lifetime = shortened_lifetime_seconds(baseline)
        log.info(f"Gold took {baseline:.0f}s; Polaris tokens now live {lifetime}s.")
        _set_lifetime(cfg, f"PT{lifetime}S")
        try:
            elapsed = gold_seconds()
        finally:
            _set_lifetime(cfg, None)
            retry_until_deadline(lambda: check_trino_write(cfg))
    if elapsed <= lifetime:
        raise E2EError(
            f"the Gold build took {elapsed:.0f}s, within the {lifetime}s token lifetime: it proved nothing. "
            "Rerun, or lower LIFETIME_FRACTION if the runner is fast."
        )
    log.info(f"Gold built in {elapsed:.0f}s against a {lifetime}s token lifetime.")


def _set_lifetime(cfg: E2EConfig, value: str | None) -> None:
    deployment = ["deployment", "-l", POLARIS_POD_SELECTOR, "-n", cfg.platform_namespace]
    kubectl(cfg, ["set", "env", *deployment, f"{LIFETIME_ENV}={value}" if value else f"{LIFETIME_ENV}-"])
    kubectl(cfg, ["rollout", "status", *deployment, f"--timeout={POLARIS_RESTART_TIMEOUT_SECONDS}s"])
