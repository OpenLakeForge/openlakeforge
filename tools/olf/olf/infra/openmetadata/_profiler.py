"""The Trino profiler, run as a Kubernetes Job and a daily CronJob OLF owns (ADR 0007 D15).

It is the crawl's sibling: the same pinned `ingestion-base` image, pod spec, resources, bot identity and in-pod
Polaris token minting, running `metadata profile -c` instead of `metadata ingest -c`. The workflow is built from
`resolve_profiling`, so it lists exactly the Fact and Dimension tables of products that opted in, with the metrics
their classification allows, and nothing else is read. No OpenMetadata ingestion pipeline is created.

The profiler is optional: a failure is reported and the deploy goes on, because the profile only decorates the catalog.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openlakeforge_core import (
    PROFILING_CRON,
    LakehouseDescriptorError,
    ProfilingTarget,
    load_lakehouse_inventory,
    resolve_profiling,
    scan_gold_roles,
)

from olf.common import log
from olf.infra import k8s, trino
from olf.infra.openmetadata._ingestion import (
    copy_polaris_secret,
    ingestion_cronjob,
    ingestion_image,
    om_api_url,
    server_config,
    trino_connection,
    workflow_job,
)

PROFILER_JOB_NAME = "openmetadata-trino-profiler"
# One thread and a per-workflow timeout keep a profile from loading the coordinator; the Job's own deadline is the
# hard stop and fits inside the one-hour Polaris token the pod minted.
_THREADS = 1
_WORKFLOW_TIMEOUT_SECONDS = 600
_DEADLINE_SECONDS = 1800
_WAIT_SECONDS = 900


def _exact(names: Sequence[str]) -> list[str]:
    return [f"^{re.escape(name)}$" for name in names]


def profiler_config(
    environ: Mapping[str, str], *, service: str, om_api: str, target: ProfilingTarget
) -> dict[str, Any]:
    """The workflow `metadata profile -c` runs for one product: its schema, its tables, its metrics.

    Every key is one of `DatabaseServiceProfilerPipeline` in 1.13.6. There is no sample-data option in that model and
    no row sampling is configured: a sample would make min, max and mean describe the sample, not the table.
    """
    return {
        "source": {
            "type": "trino",
            "serviceName": service,
            "serviceConnection": {"config": trino_connection(environ)},
            "sourceConfig": {
                "config": {
                    "type": "Profiler",
                    "computeTableMetrics": True,
                    "computeColumnMetrics": True,
                    "metrics": list(target.metrics),
                    "threadCount": _THREADS,
                    "timeoutSeconds": _WORKFLOW_TIMEOUT_SECONDS,
                    "includeViews": False,
                    "databaseFilterPattern": {"includes": _exact([environ["OPENLAKEFORGE_CATALOG_NAME"]])},
                    "schemaFilterPattern": {"includes": _exact([target.schema])},
                    "tableFilterPattern": {"includes": _exact(target.tables)},
                }
            },
        },
        "processor": {"type": "orm-profiler", "config": {}},
        "sink": {"type": "metadata-rest", "config": {}},
        "workflowConfig": {**server_config(om_api), "raiseOnError": True, "successThreshold": 100},
    }


def profiler_job(
    environ: Mapping[str, str],
    *,
    image: str,
    namespace: str,
    service: str,
    om_api: str,
    targets: Sequence[ProfilingTarget],
) -> dict[str, Any]:
    """One workflow per product, so a table name two products share is never matched across them."""
    return workflow_job(
        environ,
        name=PROFILER_JOB_NAME,
        container="profile",
        image=image,
        namespace=namespace,
        workflows={
            f"OM_WORKFLOW_{index}": profiler_config(environ, service=service, om_api=om_api, target=target)
            for index, target in enumerate(targets)
        },
        verb="profile",
        backoff_limit=0,
        deadline_seconds=_DEADLINE_SECONDS,
    )


def profiler_cronjob(job: dict[str, Any], *, catalog: str) -> dict[str, Any]:
    return ingestion_cronjob(job, catalog=catalog, schedule=PROFILING_CRON)


def profiling_targets(code_root: Path) -> tuple[ProfilingTarget, ...]:
    """What the project's descriptors allow to be profiled; none when they cannot be read."""
    try:
        inventory = load_lakehouse_inventory(code_root)
    except LakehouseDescriptorError as exc:
        log.warn(f"Profiling skipped: the lakehouse descriptors could not be read ({exc}).")
        return ()
    roles, _ = scan_gold_roles(code_root / "gold", [product.id for product in inventory.products])
    return resolve_profiling(inventory.products, roles)


def run_trino_profiling(
    environ: Mapping[str, str], *, namespace: str, service: str, targets: Sequence[ProfilingTarget]
) -> bool:
    """Profile this stage once and keep its daily CronJob; never raises for a profiler problem.

    A stage with nothing to profile gets no Job, and a CronJob left by an earlier policy is removed, so turning
    profiling off stops the reads. Returns whether the profile ran to completion (True when there was nothing to run).
    """
    job_namespace = trino.service_namespace(environ, namespace)
    catalog = environ["OPENLAKEFORGE_CATALOG_NAME"]
    cron_name = f"{PROFILER_JOB_NAME}-{catalog.replace('_', '-')}"
    try:
        if not targets:
            k8s.delete_cronjob(cron_name, job_namespace, environ=environ)
            return True
        if environ.get("OPENLAKEFORGE_CATALOG_TOKEN_URI"):
            copy_polaris_secret(namespace, job_namespace, environ=environ)
        job = profiler_job(
            environ,
            image=ingestion_image(environ),
            namespace=job_namespace,
            service=service,
            om_api=om_api_url(environ, namespace),
            targets=targets,
        )
        k8s.apply_manifest(profiler_cronjob(job, catalog=catalog), environ=environ)
        k8s.run_job(job, timeout_seconds=_WAIT_SECONDS, environ=environ)
    except k8s.KubectlError as exc:
        log.warn(
            f"The OpenMetadata profile of {catalog} did not complete; statistics stay as they were and will show "
            f"as stale. The deploy continues. {exc}"
        )
        return False
    return True
