"""The Trino metadata ingestion, run as a Kubernetes Job and hourly CronJob OLF owns.

OpenMetadata's pipeline client would generate the same workflow, but its
ingestion pods cannot mount the Trino CA (ADR 0016), so OLF builds the config
and the Job itself and runs the pinned `ingestion-base` image's `metadata
ingest` entry point.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from olf import config, k8s

JOB_NAME = "openmetadata-trino-ingestion"
REFRESH_SCHEDULE = "0 * * * *"
_CA_DIR = "/etc/olf/query-ca"
# The bot JWT is the only secret. It reaches the container through a
# secretKeyRef and is substituted into the config inside the pod, so neither
# the Job spec nor Terraform state holds it.
_TOKEN_ENV = "OM_BOT_JWT"
_TOKEN_PLACEHOLDER = "__OM_BOT_JWT__"
# The `openmetadata` principal's password (ADR 0016) travels the same way. Its
# Secret is in the namespace Trino runs in, named from the stage credential's.
_PASSWORD_ENV = "TRINO_PASSWORD"
_PASSWORD_PLACEHOLDER = "__TRINO_PASSWORD__"


def _password_secret(environ: Mapping[str, str]) -> str:
    credential = environ.get("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "")
    return f"{credential}-openmetadata" if credential else ""


def _tls(environ: Mapping[str, str]) -> bool:
    keys = ("OPENLAKEFORGE_QUERY_TRINO_TLS_PORT", "OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP")
    return all(environ.get(key) for key in keys)


def ingestion_config(environ: Mapping[str, str], *, service: str, om_api: str) -> dict[str, Any]:
    """The workflow `metadata ingest -c` runs, as the pipeline client generates it."""
    tls = _tls(environ)
    port = environ["OPENLAKEFORGE_QUERY_TRINO_TLS_PORT"] if tls else environ["OPENLAKEFORGE_QUERY_TRINO_PORT"]
    connection: dict[str, Any] = {
        "type": "Trino",
        "hostPort": f"{environ['OPENLAKEFORGE_QUERY_TRINO_HOST']}:{port}",
        "username": "openmetadata",
    }
    if tls:
        connection["connectionArguments"] = {"http_scheme": "https", "verify": f"{_CA_DIR}/ca.crt"}
    if _password_secret(environ):
        connection["authType"] = {"password": _PASSWORD_PLACEHOLDER}
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
                    # Nothing is marked deleted: `deploy-metadata` seeds table
                    # stubs before their first run materializes them (#131).
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
    container: dict[str, Any] = {
        "name": "ingest",
        "image": image,
        # The pipeline client's values (openmetadata.yaml); without them the pod is BestEffort.
        "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
        "command": [
            "sh",
            "-c",
            f"printf '%s' \"$OM_WORKFLOW\" | sed -e \"s|{_TOKEN_PLACEHOLDER}|${_TOKEN_ENV}|\""
            f" -e \"s|{_PASSWORD_PLACEHOLDER}|${{{_PASSWORD_ENV}:-}}|\" > /tmp/workflow.yaml"
            " && metadata ingest -c /tmp/workflow.yaml",
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
    if password_secret := _password_secret(environ):
        container["env"].append(
            {
                "name": _PASSWORD_ENV,
                "valueFrom": {"secretKeyRef": {"name": password_secret, "key": "password"}},
            }
        )
    pod: dict[str, Any] = {"restartPolicy": "Never", "containers": [container]}
    if _tls(environ):
        container["volumeMounts"] = [{"name": "query-ca", "mountPath": _CA_DIR, "readOnly": True}]
        pod["volumes"] = [
            {"name": "query-ca", "configMap": {"name": environ["OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP"]}}
        ]
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


def run_trino_ingestion(environ: Mapping[str, str], *, namespace: str, service: str) -> None:
    catalog = yaml.safe_load((config.distribution_root() / "release/component-catalog.yaml").read_text())
    om_host = f"{config.env('OPENMETADATA_SERVICE', 'openmetadata')}:{config.env('OPENMETADATA_SERVICE_PORT', '8585')}"
    job = ingestion_job(
        environ,
        image=catalog["components"]["images"]["openmetadata_ingestion"],
        namespace=namespace,
        service=service,
        om_api=f"http://{om_host}/api",
    )
    # Applied first so the schedule survives a failed first crawl.
    k8s.apply_manifest(ingestion_cronjob(job, catalog=environ["OPENLAKEFORGE_CATALOG_NAME"]))
    k8s.run_job(job, timeout_seconds=1800)
