"""Operator replay of quality results into OpenMetadata, without rerunning the pipeline (#797, ADR 0007).

The same `QualityPublisher` the periodic reconciler runs, driven by hand: for a run that failed to publish, a run
older than the reconciler's window, or results given up on. A governance-disabled stage is a no-op that touches
neither the cluster nor the bucket, so Slim needs no credential for this command.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from openlakeforge_core.quality_sync import OmStatus
from openlakeforge_services.openmetadata import OpenMetadataClient
from openlakeforge_services.quality_publisher import DEFAULT_LIMIT, QualityCatalog, QualityPublisher, ReconcileReport

from olf.common import config

MAX_LIMIT = 200


class QualitySyncError(RuntimeError):
    """The replay cannot start: a missing contract value or an argument out of range."""


@dataclass(frozen=True)
class QualitySyncOutcome:
    stage: str
    governance_enabled: bool
    report: ReconcileReport | None = None


def run_quality_sync(
    environ: Mapping[str, str],
    *,
    open_store: Callable[[str], AbstractContextManager[Any]],
    open_catalog: Callable[[], AbstractContextManager[OpenMetadataClient]],
    limit: int = DEFAULT_LIMIT,
    run_id: str | None = None,
    retry_failed: bool = False,
    warn_status: OmStatus = OmStatus.ABORTED,
) -> QualitySyncOutcome:
    """`open_store(bucket)` and `open_catalog()` are entered only once governance is known to be enabled."""
    if not 1 <= limit <= MAX_LIMIT:
        raise QualitySyncError(f"--limit must be between 1 and {MAX_LIMIT}")
    catalog_name = config.env("OPENLAKEFORGE_CATALOG_NAME", environ=environ)
    if not catalog_name.startswith("lakehouse_"):
        raise QualitySyncError(f"catalog {catalog_name!r} is not a stage catalog (lakehouse_<stage>)")
    stage = catalog_name.removeprefix("lakehouse_")
    if not config.truthy(config.env("OPENLAKEFORGE_GOVERNANCE_ENABLED", "false", environ=environ)):
        return QualitySyncOutcome(stage=stage, governance_enabled=False)

    base_uri = config.env("OPENLAKEFORGE_ARTIFACT_BASE_URI", environ=environ)
    bucket = config.env("OPENLAKEFORGE_OPS_BUCKET_NAME", environ=environ)
    service = config.env("OPENMETADATA_CATALOG_SERVICE", environ=environ) or config.env(
        "OPENLAKEFORGE_CATALOG_PROVIDER", environ=environ
    )
    if not (base_uri and bucket and service):
        raise QualitySyncError("the contract environment has no artifact base URI, ops bucket or catalog service")
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", environ=environ) or f"{service}.{catalog_name}"
    with open_store(bucket) as store, open_catalog() as client:
        publisher = QualityPublisher(
            store=store,
            base_uri=base_uri,
            stage=stage,
            catalog=QualityCatalog(client),
            database_fqn=database_fqn,
            silver_schemas=schema_map("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON", environ),
            gold_schemas=schema_map("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", environ),
            warn_status=warn_status,
        )
        report = publisher.reconcile(limit=limit, run_id=run_id, retry_failed=retry_failed)
    return QualitySyncOutcome(stage=stage, governance_enabled=True, report=report)


def schema_map(name: str, environ: Mapping[str, str]) -> dict[str, str] | None:
    """The provider contract's logical name -> physical schema FQN, as `deploy-metadata` registers tables under."""
    raw = config.env(name, environ=environ)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QualitySyncError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise QualitySyncError(f"{name} must map names to schema FQNs")
    return value
