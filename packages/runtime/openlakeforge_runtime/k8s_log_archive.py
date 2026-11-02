from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime

from kubernetes import client, config

from openlakeforge_runtime.s3_artifacts import s3_client, split_s3_uri


def main() -> None:
    namespace = os.environ.get("OPENLAKEFORGE_KUBE_NAMESPACE", "olf-dev")
    since_seconds = int(os.environ.get("OPENLAKEFORGE_LOG_ARCHIVE_SINCE_SECONDS", "3600"))
    log_base_uri = os.environ.get("OPENLAKEFORGE_LOG_BASE_URI", "s3://openlakeforge-ops/logs")

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    core = client.CoreV1Api()
    archived_at = datetime.now(UTC)
    records: list[dict] = []

    pods = core.list_namespaced_pod(namespace=namespace)
    for pod in pods.items:
        pod_name = pod.metadata.name
        container_statuses = pod.status.container_statuses or []
        container_names = [status.name for status in container_statuses]
        if not container_names:
            container_names = [container.name for container in pod.spec.containers]

        for container_name in container_names:
            try:
                text = core.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    container=container_name,
                    since_seconds=since_seconds,
                    timestamps=True,
                )
                for line in text.splitlines():
                    records.append(
                        {
                            "schema": "openlakeforge.k8s_log.v1",
                            "archived_at": archived_at.isoformat(),
                            "namespace": namespace,
                            "pod": pod_name,
                            "container": container_name,
                            "line": line,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                records.append(
                    {
                        "schema": "openlakeforge.k8s_log.v1",
                        "archived_at": archived_at.isoformat(),
                        "namespace": namespace,
                        "pod": pod_name,
                        "container": container_name,
                        "archive_error": str(exc),
                    }
                )

    if not records:
        records.append(
            {
                "schema": "openlakeforge.k8s_log.v1",
                "archived_at": archived_at.isoformat(),
                "namespace": namespace,
                "message": "no pod log lines returned",
            }
        )

    payload = gzip.compress(
        ("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n").encode("utf-8")
    )
    bucket, prefix = split_s3_uri(log_base_uri)
    date_part = archived_at.strftime("%Y-%m-%d")
    hour_part = archived_at.strftime("%H")
    stamp = archived_at.strftime("%Y%m%dT%H%M%SZ")
    key = "/".join(
        part.strip("/")
        for part in (
            prefix,
            "k8s",
            f"namespace={namespace}",
            f"date={date_part}",
            f"hour={hour_part}",
            f"k8s-logs-{stamp}.ndjson.gz",
        )
        if part.strip("/")
    )
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/x-ndjson",
        ContentEncoding="gzip",
    )
    print(f"Archived {len(records)} Kubernetes log records to s3://{bucket}/{key}")


if __name__ == "__main__":
    main()
