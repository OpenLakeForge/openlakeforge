"""A Gold build that outlasts the Polaris token (#1061).

A dbt process holds the Polaris token it started with and Trino hands it to Polaris at every commit, so a
build whose commits run past the token's lifetime failed at its first commit after expiry. The proof cannot
wait the default hour: it runs the product's Gold models once to time them, shortens Polaris's token lifetime
to a fraction of that time, and runs them again. The runtime logs the moment it mints each token into the
Dagster run, and Iceberg records when each commit landed. The second run must pass, and a Gold table must
have been committed after the first token's lifetime ran out: a run that merely lasted longer than the
lifetime (Dagster start-up, a short dbt process) proves nothing about the tokens.

The lifetime is set on the running Polaris Deployment for the length of the check and removed afterwards.
It is not a profile field and no Terraform input: a stack must never be configured with tokens this short.
"""

from __future__ import annotations

import re
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

from openlakeforge_core import Product

from olf.common import log
from olf.infra.e2e._dagster import dagster_client, launch_and_poll
from olf.infra.e2e._maintenance import base_tables
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise
from olf.infra.e2e._trino import (
    POLARIS_POD_SELECTOR,
    POLARIS_RESTART_TIMEOUT_SECONDS,
    _glue_namespace_prefix,
    check_trino_write,
    retry_until_deadline,
    stage_catalog_name,
    trino_query,
)

# Quarkus maps `polaris.authentication.token-broker.max-token-generation` (Polaris 1.8, default PT1H) to this.
LIFETIME_ENV = "POLARIS_AUTHENTICATION_TOKEN_BROKER_MAX_TOKEN_GENERATION"
# The Dagster log line the runtime writes when a Gold build mints a token (polaris_token.TOKEN_MINTED_MESSAGE;
# a runtime test pins the two together).
TOKEN_MINTED_MESSAGE = "Minted a Polaris token for a dbt build generation"
# The runtime builds one dbt generation (models whose upstream models are all built) per token, so each
# generation's own dbt process (start-up and build) must fit inside the lifetime: it is the baseline run's slowest
# generation with headroom. The measured run then pauses that long between generations (the runtime's
# GENERATION_PAUSE_TAG), so its last commit lands after the first token expired whatever the build's size. A
# product with a single generation cannot prove anything, so the check skips it.
GENERATION_HEADROOM = 1.5
# polaris_token.GENERATION_PAUSE_TAG; a runtime test pins the two together.
GENERATION_PAUSE_TAG = "openlakeforge/e2e-generation-pause-seconds"
MIN_LIFETIME_SECONDS = 15
_REF = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""")
# SQL and Jinja comments, which may mention `ref(...)` without it being a dependency.
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/|\{#.*?#\}", re.DOTALL)
# The catalog implementations backed by Polaris, whose Deployment this check reconfigures:
# `catalog.iceberg_rest.polaris` (local) and `catalog.iceberg_rest.polaris_on_aks` (Azure). Glue mints no tokens.
POLARIS_CATALOG_PREFIX = "catalog.iceberg_rest.polaris"

_RUN_LOG_TIMES = """
query RunLogTimes($runId: ID!) {
  logsForRun(runId: $runId) {
    __typename
    ... on EventConnection { events { __typename ... on MessageEvent { message timestamp } } }
    ... on PythonError { message }
  }
}
"""


def shortened_lifetime_seconds(mints: list[float], ended: float) -> int | None:
    """A token lifetime that each generation of the baseline run fits in, with headroom.

    `mints` are the baseline run's token mints (one per generation), `ended` its last Gold commit, both in Unix
    seconds. None for a single generation, which no pause between generations can stretch.
    """
    if len(mints) < 2:
        return None
    ordered = sorted(mints)
    slowest = max(later - earlier for earlier, later in zip(ordered, [*ordered[1:], ended], strict=True))
    return max(MIN_LIFETIME_SECONDS, int(slowest * GENERATION_HEADROOM) + 1)


def gold_models(models_dir: Path) -> dict[str, set[str]]:
    """The `.sql` models under `models_dir` (named as dbt names them, so as their asset keys are) and their `ref()`s.

    Comments are dropped first, and a model never depends on itself, so a `ref()` in a comment cannot make a cycle.
    """
    sources = {path.stem: _COMMENT.sub("", path.read_text()) for path in models_dir.rglob("*.sql")}
    return {
        name: {ref for ref in _REF.findall(sql) if ref in sources and ref != name} for name, sql in sources.items()
    }


def generation_count(models_dir: Path) -> int:
    """How many topological generations the `ref()` graph of the `.sql` models under `models_dir` has."""
    sorter = TopologicalSorter(gold_models(models_dir))
    sorter.prepare()
    generations = 0
    while sorter.is_active():
        sorter.done(*sorter.get_ready())
        generations += 1
    return generations


def catalog_is_polaris(implementation: object) -> bool:
    return isinstance(implementation, str) and implementation.startswith(POLARIS_CATALOG_PREFIX)


def uses_polaris(cfg: E2EConfig) -> bool:
    stages = load_provider_contracts_or_raise(cfg)["stages"]
    return catalog_is_polaris(next(iter(stages.values()))["catalog"].get("implementation"))


def mint_times(events: list[dict[str, Any]]) -> list[float]:
    """The Unix seconds at which the runtime logged a token mint, from a run's Dagster events."""
    return sorted(int(event["timestamp"]) / 1000 for event in events if event.get("message") == TOKEN_MINTED_MESSAGE)


def proof_product(cfg: E2EConfig) -> Product | None:
    """The default product if its Gold builds in two or more generations, else the first product that does."""
    default = cfg.inventory.default_product
    candidates = [default, *(product for product in cfg.inventory.products if product.id != default.id)]
    for product in candidates:
        if generation_count(cfg.repo_root / product.dbt_project_dir / "models") >= 2:
            return product
    return None


def check_gold_outlasts_polaris_token(cfg: E2EConfig) -> None:
    if not uses_polaris(cfg):
        log.info("Skipping the token lifetime proof: the stage's catalog is not Polaris, so it mints no tokens.")
        return
    product = proof_product(cfg)
    if product is None:
        log.info(
            "Skipping the token lifetime proof: every product's Gold builds in one dbt generation, one process for "
            "the whole build, so no shortened lifetime can be outlasted by it. It needs a product with two."
        )
        return
    models_dir = cfg.repo_root / product.dbt_project_dir / "models"
    generations = generation_count(models_dir)
    selection = [(product.id, model) for model in sorted(gold_models(models_dir))]
    log.step(f"Building {product.id}'s Gold models past a shortened Polaris token lifetime...")
    with dagster_client(cfg) as client:

        def gold_run(tags: dict[str, str] | None = None) -> tuple[str, float]:
            run_id = launch_and_poll(cfg, client, product.job_name, tags=tags, asset_selection=selection)
            run = client.get_run(run_id)
            if run is None or run.started_at is None or run.ended_at is None:
                raise E2EError(f"Dagster recorded no start and end time for the {product.id} Gold run.")
            return run_id, float(run.ended_at)

        def last_commit() -> float:
            schema = f"{_glue_namespace_prefix(cfg)}{product.gold_namespace}"
            return _last_gold_commit(cfg, [(product.gold_namespace, table) for table in base_tables(cfg, schema)])

        baseline_id, _ = gold_run()
        baseline_mints = mint_times(_run_events(client, baseline_id))
        # The token-exposed interval ends at the last commit, not the run's end: Dagster spends time after dbt
        # (artifact upload, teardown) that no token covers, and counting it would oversize the last generation.
        baseline_commit = last_commit()
        lifetime = shortened_lifetime_seconds(baseline_mints, baseline_commit)
        if lifetime is None:
            log.info(
                f"Skipping the token lifetime proof: {product.id}'s mints at "
                f"{[round(m - baseline_mints[0]) for m in baseline_mints]}s and last commit at "
                f"{round(baseline_commit - baseline_mints[0]) if baseline_mints else None}s: it needs two generations."
            )
            return
        log.info(f"Tokens now live {lifetime}s (the slowest generation fits) and generations wait as long.")
        try:
            _set_lifetime(cfg, f"PT{lifetime}S")
            # The restarted Polaris is Ready before kindnet admits traffic to it (see _trino.py); wait until the
            # catalog answers, or the first generation's mint could fail on a healthy runtime.
            retry_until_deadline(lambda: check_trino_write(cfg))
            # Pausing a lifetime between generations makes the first token expire before the last commit whatever
            # the build's size; a runtime that minted once per build would fail that commit.
            run_id, _ = gold_run({GENERATION_PAUSE_TAG: str(lifetime)})
        finally:
            _set_lifetime(cfg, None)
            retry_until_deadline(lambda: check_trino_write(cfg))
        minted = mint_times(_run_events(client, run_id))
        committed = last_commit()
    if len(minted) < generations:
        raise E2EError(f"the Gold run minted {len(minted)} token(s) for {generations} dbt generations.")
    expired = minted[0] + lifetime
    if committed <= expired:
        raise E2EError(
            f"the last Gold commit landed {expired - committed:.0f}s before the first token's {lifetime}s lifetime "
            "ran out: the pause between generations did not take effect, so it proved nothing."
        )
    log.info(f"Gold committed {committed - expired:.0f}s after its first token expired ({lifetime}s lifetime).")


def _run_events(client: Any, run_id: str) -> list[dict[str, Any]]:
    result = client.graphql(_RUN_LOG_TIMES, {"runId": run_id})["logsForRun"]
    if result.get("__typename") != "EventConnection":
        raise E2EError(f"Dagster could not read the events of run {run_id}: {result.get('message', result)}")
    return list(result["events"])


def _last_gold_commit(cfg: E2EConfig, tables: list[tuple[str, str]]) -> float:
    """Unix seconds of the newest snapshot of any of `tables`, which Iceberg stamps when Polaris accepts a commit."""
    prefix = _glue_namespace_prefix(cfg)
    catalog = stage_catalog_name(cfg)
    selects = [
        f'SELECT to_unixtime(max(committed_at)) FROM {catalog}."{prefix}{schema}"."{table}$snapshots"'
        for schema, table in tables
    ]
    values = [float(line) for line in trino_query(cfg, " UNION ALL ".join(selects)).splitlines() if line.strip()]
    if not values:
        raise E2EError("Trino returned no snapshot times for the Gold tables.")
    return max(values)


def _set_lifetime(cfg: E2EConfig, value: str | None) -> None:
    deployment = ["deployment", "-l", POLARIS_POD_SELECTOR, "-n", cfg.platform_namespace]
    kubectl(cfg, ["set", "env", *deployment, f"{LIFETIME_ENV}={value}" if value else f"{LIFETIME_ENV}-"])
    kubectl(cfg, ["rollout", "status", *deployment, f"--timeout={POLARIS_RESTART_TIMEOUT_SECONDS}s"])
