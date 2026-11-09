"""The Trino metadata ingestion, run as a Kubernetes Job and hourly CronJob OLF owns.

OpenMetadata's pipeline client would generate the same workflow, but its
ingestion pods cannot mount the Trino CA (ADR 0016), so OLF builds the config
and the Job itself and runs the pinned `ingestion-base` image's `metadata
ingest` entry point.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

import yaml

from olf.common import config
from olf.infra import k8s, trino

JOB_NAME = "openmetadata-trino-ingestion"
REFRESH_SCHEDULE = "0 * * * *"
# The bot JWT is the only secret. It reaches the container through a
# secretKeyRef and is substituted into the config inside the pod, so neither
# the Job spec nor Terraform state holds it.
_TOKEN_ENV = "OM_BOT_JWT"
_TOKEN_PLACEHOLDER = "__OM_BOT_JWT__"
# Trino carries no policy of its own: the crawl presents the `openmetadata` Polaris principal's token as the `token`
# extra credential (ADR 0019 section 9). The pod mints it from the principal's client credentials just before the
# crawl, so the token is in neither the Job spec nor the stored service; the 3600 s deadline fits its one hour.
POLARIS_SECRET = "polaris-om-creds"
_POLARIS_TOKEN_PLACEHOLDER = "__POLARIS_TOKEN__"
_MINT_ENV = "OM_POLARIS_MINT"
_MINT = (
    "import json,os,urllib.parse as p,urllib.request as r;"
    'e=os.environ;d=p.urlencode({"grant_type":"client_credentials","client_id":e["POLARIS_CLIENT_ID"],'
    '"client_secret":e["POLARIS_CLIENT_SECRET"],"scope":e["POLARIS_SCOPE"]}).encode();'
    'print(json.load(r.urlopen(e["POLARIS_TOKEN_URI"],d,timeout=30))["access_token"])'
)


def _polaris(environ: Mapping[str, str]) -> bool:
    return bool(environ.get("OPENLAKEFORGE_CATALOG_TOKEN_URI"))


def ingestion_config(environ: Mapping[str, str], *, service: str, om_api: str) -> dict[str, Any]:
    """The workflow `metadata ingest -c` runs, as the pipeline client generates it."""
    port = environ["OPENLAKEFORGE_QUERY_TRINO_PORT"]
    connection: dict[str, Any] = {
        "type": "Trino",
        "hostPort": f"{environ['OPENLAKEFORGE_QUERY_TRINO_HOST']}:{port}",
        "username": "openmetadata",
    }
    arguments: dict[str, Any] = {}
    if _polaris(environ):
        arguments["extra_credential"] = [["token", _POLARIS_TOKEN_PLACEHOLDER]]
    if arguments:
        connection["connectionArguments"] = arguments
    return {
        "source": {
            "type": "trino",
            "serviceName": service,
            "serviceConnection": {"config": connection},
            "sourceConfig": {
                "config": {
                    "type": "DatabaseMetadata",
                    "includeViews": False,
                    "includeTags": False,
                    # Trino lists every catalog; only this stage's is crawled.
                    # Nothing is marked deleted: `deploy-metadata` seeds the
                    # domain-tagged schemas before their first run materializes tables (#131).
                    "databaseFilterPattern": {"includes": [f"^{environ['OPENLAKEFORGE_CATALOG_NAME']}$"]},
                    "markDeletedDatabases": False,
                    "markDeletedSchemas": False,
                    "markDeletedTables": False,
                }
            },
        },
        "sink": {"type": "metadata-rest", "config": {}},
        "workflowConfig": {
            "openMetadataServerConfig": {
                "hostPort": om_api,
                "authProvider": "openmetadata",
                "securityConfig": {"jwtToken": _TOKEN_PLACEHOLDER},
            }
        },
    }


def ingestion_job(
    environ: Mapping[str, str], *, image: str, namespace: str, service: str, om_api: str
) -> dict[str, Any]:
    workflow = ingestion_config(environ, service=service, om_api=om_api)
    mint = f'POLARIS_JWT=$(python -c "${_MINT_ENV}") && ' if _polaris(environ) else ""
    container: dict[str, Any] = {
        "name": "ingest",
        "image": image,
        # The pipeline client's values (openmetadata.yaml); without them the pod is BestEffort.
        "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
        "command": [
            "sh",
            "-c",
            f'{mint}printf \'%s\' "$OM_WORKFLOW" | sed -e "s|{_TOKEN_PLACEHOLDER}|${_TOKEN_ENV}|"'
            + (f' -e "s|{_POLARIS_TOKEN_PLACEHOLDER}|$POLARIS_JWT|"' if mint else "")
            + " > /tmp/workflow.yaml && metadata ingest -c /tmp/workflow.yaml",
        ],
        "env": [
            {"name": "OM_WORKFLOW", "value": yaml.safe_dump(workflow)},
            {
                "name": _TOKEN_ENV,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": environ["OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME"],
                        "key": environ["OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY"],
                    }
                },
            },
        ],
    }
    if mint:
        scope = environ.get("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE") or "PRINCIPAL_ROLE:ALL"
        container["env"] += [
            {"name": _MINT_ENV, "value": _MINT},
            {"name": "POLARIS_TOKEN_URI", "value": environ["OPENLAKEFORGE_CATALOG_TOKEN_URI"]},
            {"name": "POLARIS_SCOPE", "value": scope},
        ]
        for name in ("CLIENT_ID", "CLIENT_SECRET"):
            ref = {"secretKeyRef": {"name": POLARIS_SECRET, "key": f"POLARIS_OM_{name}"}}
            container["env"].append({"name": f"POLARIS_{name}", "valueFrom": ref})
    pod: dict[str, Any] = {"restartPolicy": "Never", "containers": [container]}
    labels = {"openlakeforge.io/job": JOB_NAME}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": JOB_NAME, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 1,
            "activeDeadlineSeconds": 3600,
            "ttlSecondsAfterFinished": 3600,
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }


def ingestion_cronjob(job: dict[str, Any], *, catalog: str) -> dict[str, Any]:
    """The hourly refresh: `job`'s pod, spec and labels, so the two cannot diverge.

    Built here rather than in Terraform so the workflow config exists once; the
    TLS and bot-Secret facts it needs are the deployed contracts', which
    `deploy-metadata` already holds. The workflow crawls one stage's catalog,
    so each governed stage gets its own CronJob, named by that catalog.
    """
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {**job["metadata"], "name": f"{JOB_NAME}-{catalog.replace('_', '-')}"},
        "spec": {
            "schedule": REFRESH_SCHEDULE,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {"spec": job["spec"]},
        },
    }


def _copy_polaris_secret(source: str, target: str, *, environ: Mapping[str, str]) -> None:
    """The `openmetadata` principal's Secret lives with Polaris; a secretKeyRef cannot cross namespaces."""
    if source == target:
        return
    data = k8s.secret_data(POLARIS_SECRET, source, environ=environ)
    if data is None:
        raise k8s.KubectlError(f"Secret '{POLARIS_SECRET}' is missing in '{source}'.")
    k8s.delete_secret(POLARIS_SECRET, target, environ=environ)
    values = {key: base64.b64decode(value).decode("utf-8") for key, value in data.items()}
    k8s.write_secret(POLARIS_SECRET, target, values, overwrite=False, environ=environ)


def run_trino_ingestion(environ: Mapping[str, str], *, namespace: str, service: str) -> None:
    catalog = yaml.safe_load((config.distribution_root(environ=environ) / "release/component-catalog.yaml").read_text())
    # `namespace` is OpenMetadata's. The Job runs beside the stage's coordinator, where the `openmetadata`
    # principal's Secret is (a secretKeyRef cannot cross namespaces), so OpenMetadata is addressed with its own.
    om_host = (
        f"{config.env('OPENMETADATA_SERVICE', 'openmetadata', environ=environ)}.{namespace}:"
        f"{config.env('OPENMETADATA_SERVICE_PORT', '8585', environ=environ)}"
    )
    job_namespace = trino.service_namespace(environ, namespace)
    if _polaris(environ):
        _copy_polaris_secret(namespace, job_namespace, environ=environ)
    job = ingestion_job(
        environ,
        image=catalog["components"]["images"]["openmetadata_ingestion"],
        namespace=job_namespace,
        service=service,
        om_api=f"http://{om_host}/api",
    )
    # Applied first so the schedule survives a failed first crawl.
    k8s.apply_manifest(ingestion_cronjob(job, catalog=environ["OPENLAKEFORGE_CATALOG_NAME"]), environ=environ)
    k8s.run_job(job, timeout_seconds=1800, environ=environ)
