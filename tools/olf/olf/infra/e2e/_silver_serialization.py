"""Silver write serialization proof: two products of one domain, launched together, run one after the other.

Products of a domain read and overwrite the same Silver tables. The run queue admits one run per domain
(`tagConcurrencyLimits` in infra/helm/values/local/dagster.yaml, keyed on the tag the product job carries); if that
limit is missing or the tag drops off the job, both runs start at once and their Floe Jobs commit to one Iceberg table
together.
"""

from __future__ import annotations

from collections.abc import Sequence

from openlakeforge_core import Product
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterRun

from olf.common import log
from olf.infra.e2e._dagster import DAGSTER_JOB_TIMEOUT_SECONDS, dagster_client
from olf.infra.e2e._shell import E2EConfig, E2EError

# openlakeforge_runtime.product_dagster.SILVER_DOMAIN_TAG; a test keeps the two equal.
SILVER_DOMAIN_TAG = "openlakeforge/silver-domain"


def check_silver_writes_serialized(cfg: E2EConfig) -> None:
    pair = _two_products_of_one_domain(cfg.inventory.products)
    if pair is None:
        log.info("No domain has two products; nothing shares Silver tables.")
        return
    first, second = pair
    log.step(f"Launching {first.job_name} and {second.job_name} together; they share the {first.domain_name} Silver.")
    with dagster_client(cfg) as client:
        run_ids = [client.launch(product.job_name) for product in pair]
        for product, run_id in zip(pair, run_ids, strict=True):
            _wait(client, product.job_name, run_id)
        runs = [_finished_run(client, run_id) for run_id in run_ids]
    assert_serialized(runs, domain=first.domain_name)


# The second run waits for the first, so it needs twice a single run's budget.
_QUEUED_RUN_TIMEOUT_SECONDS = 2 * DAGSTER_JOB_TIMEOUT_SECONDS


def _wait(client: DagsterClient, job: str, run_id: str) -> None:
    # launch_and_poll starts its own run; these two must be in flight together, so only the wait is shared.
    try:
        client.poll(job, run_id, timeout_seconds=_QUEUED_RUN_TIMEOUT_SECONDS, on_status=log.info)
    except ServiceClientError as exc:
        raise E2EError(str(exc)) from exc


def assert_serialized(runs: Sequence[DagsterRun], *, domain: str) -> None:
    for run in runs:
        if run.tags.get(SILVER_DOMAIN_TAG) != domain:
            raise E2EError(f"{run.job_name} run {run.run_id} lacks the {SILVER_DOMAIN_TAG}={domain} tag")
        if run.started_at is None or run.ended_at is None:
            raise E2EError(f"{run.job_name} run {run.run_id} has no start or end time")
    ordered = sorted(runs, key=lambda run: run.started_at or 0.0)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if (earlier.ended_at or 0.0) > (later.started_at or 0.0):
            raise E2EError(
                f"{earlier.job_name} ({earlier.run_id}) and {later.job_name} ({later.run_id}) overlapped: "
                f"the first ended at {earlier.ended_at}, the second started at {later.started_at}. "
                f"Both write {domain} Silver tables."
            )


def _finished_run(client: DagsterClient, run_id: str) -> DagsterRun:
    run = client.get_run(run_id)
    if run is None:
        raise E2EError(f"Dagster has no run {run_id}")
    return run


def _two_products_of_one_domain(products: Sequence[Product]) -> tuple[Product, Product] | None:
    by_domain: dict[str, list[Product]] = {}
    for product in products:
        by_domain.setdefault(product.domain_name, []).append(product)
    for members in by_domain.values():
        if len(members) >= 2:
            return members[0], members[1]
    return None
