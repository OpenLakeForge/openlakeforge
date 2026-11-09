"""The whole OpenMetadata bootstrap, and the inputs the platform phase reads for it from the contract (#562).

`olf infra deploy` runs it after the platform apply; `olf infra openmetadata bootstrap` runs it with
the inputs given as flags. Every step is idempotent, so a rerun converges.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from olf.common.profile import StageName
from olf.common.provider_contracts import ProviderContracts
from olf.infra.access import Route
from olf.infra.openmetadata import (
    _bootstrap,
    _catalog_seed,
    _search_reindex,
    _signup_baseline,
    _stage_services,
    _stale_services,
)
from olf.infra.openmetadata._catalog_seed import CatalogSeed
from olf.infra.openmetadata._stage_services import StageServices

CATALOG_DISPLAY = "Lakehouse catalogs (Trino)"
# Each stage's in-cluster Services, as the dagster and superset modules name them; OpenMetadata
# ingestion runs in the cluster, so it is given these rather than the ingress routes.
_DAGSTER_WEBSERVER = "dagster-dagster-webserver"
_SUPERSET = "superset"


def run_bootstrap(
    route: Route,
    namespace: str,
    *,
    email: str,
    sso: bool,
    seed: CatalogSeed | None,
    stages: Sequence[StageServices],
    environ: Mapping[str, str] | None = None,
) -> None:
    client = _bootstrap.bootstrap_auth(route, namespace, email=email, sso=sso, environ=environ)
    if seed:
        _catalog_seed.bootstrap_catalog(client, seed)
    # Gated: without stages, every stage's services would be removed as ungoverned.
    if stages:
        _stage_services.bootstrap_stage_services(client, stages, environ=environ)
    _stale_services.remove_legacy_services(client)
    if sso:
        _signup_baseline.restrict_signup_defaults(client)
    if seed:
        _search_reindex.ensure_search_indexed(client, seed)


def _trino_namespaces(query: Mapping[str, Any]) -> list[str]:
    host_port = query["endpoint"].removeprefix("http://").split("/", 1)[0]
    return [f"trino://{host_port}"]


def bootstrap_inputs(contracts: ProviderContracts) -> tuple[CatalogSeed, tuple[StageServices, ...]] | None:
    """The catalog seed and stage services for the governed stages; None when no stage is governed."""
    governed = contracts.governed_stages
    if not governed:
        return None
    # The environment roots' governance_dagster_stage: the selected stage (DEV when enabled, else
    # the first by name) when it is governed, else the first governed stage by name.
    canonical = governed.get(StageName.DEV) or governed[min(governed)]
    # Canonical stage first: its plain address is the service's connection address. A shared coordinator
    # repeats across stages, hence the dict for ordered de-duplication.
    namespaces = dict.fromkeys(
        namespace for stage in (canonical, *governed.values()) for namespace in _trino_namespaces(stage.query)
    )
    seed = CatalogSeed(
        service="aws_glue" if canonical.catalog.get("catalog_type") == "glue" else "polaris",
        display_name=CATALOG_DISPLAY,
        trino_namespaces=tuple(namespaces),
        pipeline_service=canonical.orchestration.get("pipeline_service_name", f"dagster_{canonical.name.value}"),
        databases=tuple(stage.catalog["catalog_name"] for stage in governed.values()),
    )
    stages = tuple(
        StageServices(stage.name.value, f"http://{_DAGSTER_WEBSERVER}.{stage.namespace}:80")
        if stage.reporting is None
        else StageServices(
            stage.name.value,
            f"http://{_DAGSTER_WEBSERVER}.{stage.namespace}:80",
            f"http://{_SUPERSET}.{stage.namespace}:8088",
            stage.namespace,
        )
        for stage in governed.values()
    )
    return seed, stages
