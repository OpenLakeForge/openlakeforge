"""The whole OpenMetadata bootstrap, and the inputs the platform phase reads for it from the contract (#562).

`olf infra deploy` runs it after the platform apply; `olf infra openmetadata bootstrap` runs it with
the inputs given as flags. Every step is idempotent, so a rerun converges.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.openmetadata import OpenMetadataClient

from olf.common import log
from olf.common.profile import StageName
from olf.common.provider_contracts import ProviderContracts
from olf.infra import k8s
from olf.infra.access import Route
from olf.infra.openmetadata import (
    _auth,
    _bootstrap,
    _catalog_seed,
    _quality_writer,
    _replication,
    _search_reindex,
    _signup_baseline,
    _stage_services,
    _stale_services,
)
from olf.infra.openmetadata._catalog_seed import CatalogSeed
from olf.infra.openmetadata._stage_services import STAGES, StageServices

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
        _quality_writer.ensure_quality_writers(client, route, namespace, [s.stage for s in stages], environ)
    _stale_services.remove_legacy_services(client)
    if sso:
        _signup_baseline.restrict_signup_defaults(client)
    if seed:
        _search_reindex.ensure_search_indexed(client, seed)


def _stored_admin(route: Route, namespace: str, email: str, environ: Mapping[str, str] | None) -> OpenMetadataClient:
    """An administrator from the stored credentials only: a teardown never resets the built-in admin's password."""
    token = _auth.automation_token(namespace, environ=environ)
    if token:
        return route.client(OpenMetadataClient, token=token)
    client = route.client(OpenMetadataClient)
    client.login(email, _auth.admin_password(namespace, environ=environ))
    return client


def retire_governance(
    namespace: str,
    stage_namespaces: Sequence[str],
    *,
    reach: Callable[[], AbstractContextManager[Route]] | None,
    email: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """No stage is governed: remove every credential the bootstrap issued to the stages (#1323).

    The bootstrap does not run then, so it cannot retire them a stage at a time. The roots remove
    OpenMetadata in the same apply, so `reach` is normally None and the bots are left: one that
    OpenMetadata comes back with has no Secret, so the next governed bootstrap rotates or deletes it.
    The Secrets go regardless, by exact name. When OpenMetadata still answers, the quality-writer
    bots go too, and a failure there only warns: it must not keep the Secrets alive or fail a
    deploy that is removing OpenMetadata.
    The ingestion bot is OpenMetadata's own, so only its Secret and the stages' copies go.
    """
    if reach is not None:
        try:
            with reach() as route:
                client = _stored_admin(route, namespace, email, environ)
                for stage in STAGES:
                    _quality_writer.remove_writer_bot(client, stage)
        except (ServiceClientError, k8s.KubectlError) as exc:
            log.warn(f"Could not reach OpenMetadata to delete the quality-writer bots; removing their Secrets: {exc}")
    for stage in STAGES:
        _quality_writer.remove_writer_secret(namespace, stage, environ)
    _replication.revoke_ingestion_bot([namespace, *stage_namespaces], environ=environ)


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
