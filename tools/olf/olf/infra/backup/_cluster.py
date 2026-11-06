"""`Operations` bound to a deployed local stack: kubectl for PostgreSQL, Secrets and the daemon, S3 for the buckets.

Not yet run against a live stack (#910): the pod, container and Deployment names below are the Terraform modules'.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient

from olf.common import config
from olf.common.provider_contracts import ProviderContracts
from olf.infra import k8s, s3
from olf.infra.backup import BackupError
from olf.infra.backup._backup import DAEMON_DEPLOYMENT, Operations
from olf.infra.e2e._dagster import DAGSTER_WEBSERVER_SERVICE_NAME

# modules/storage/postgresql: the StatefulSet `postgresql` runs one container of that name as the `postgres`
# superuser, and the image trusts that user over its own socket, so no password is read or passed.
POSTGRES_POD = "postgresql-0"
POSTGRES_CONTAINER = "postgresql"
POSTGRES_USER = "postgres"
# Dagster's run states before a run ends; a daemon-less stage still has the started ones running.
_IN_FLIGHT = ("QUEUED", "NOT_STARTED", "MANAGED", "STARTING", "STARTED", "CANCELING")
_IN_FLIGHT_QUERY = """
query InFlight($filter: RunsFilter) {
  runsOrError(filter: $filter, limit: 50) {
    __typename
    ... on Runs { results { runId } }
    ... on PythonError { message }
  }
}
"""


@contextmanager
def cluster_operations(
    contracts: ProviderContracts, *, shared_namespace: str, environ: Mapping[str, str]
) -> Iterator[Operations]:
    """The port-forward to object storage opens on the first bucket copy and closes with the block."""
    ops_storage = contracts.shared.values["ops_storage"]
    storage = next(iter(contracts.stages.values())).storage
    log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge", environ=environ)
    with ExitStack() as stack:
        clients: list[Any] = []

        def s3_client() -> Any:
            if not clients:
                secret = ops_storage["credentials_secret_name"]
                clients.append(
                    stack.enter_context(
                        s3.port_forward_client(
                            ops_storage["bucket_name"],
                            service=storage["s3_service_name"],
                            remote_port=int(storage["s3_service_port"]),
                            namespace=shared_namespace,
                            access_key_id=k8s.secret_value(
                                secret, ops_storage["access_key_id_key"], shared_namespace, environ=environ
                            ),
                            secret_access_key=k8s.secret_value(
                                secret, ops_storage["secret_access_key_key"], shared_namespace, environ=environ
                            ),
                            region=storage["region"],
                            environ=environ,
                        )
                    )
                )
            return clients[0]

        def runs_in_flight(namespace: str) -> list[str]:
            with k8s.port_forward(
                DAGSTER_WEBSERVER_SERVICE_NAME,
                80,
                namespace,
                log_path=f"{log_prefix}-backup-dagster.log",
                environ=environ,
            ) as port:
                try:
                    result = DagsterClient(f"http://127.0.0.1:{port}/graphql").graphql(
                        _IN_FLIGHT_QUERY, {"filter": {"statuses": list(_IN_FLIGHT)}}
                    )["runsOrError"]
                except ServiceClientError as exc:
                    raise BackupError(f"cannot list the Dagster runs in {namespace}: {exc}") from exc
            if result.get("__typename") != "Runs":
                raise BackupError(f"cannot list the Dagster runs in {namespace}: {result.get('message', result)}")
            return [run["runId"] for run in result["results"]]

        yield Operations(
            daemon_replicas=lambda namespace: k8s.deployment_replicas(DAEMON_DEPLOYMENT, namespace, environ=environ),
            scale_daemon=lambda namespace, replicas: k8s.scale_deployment(
                DAEMON_DEPLOYMENT, namespace, replicas, environ=environ
            ),
            runs_in_flight=runs_in_flight,
            dump_database=lambda name, destination: k8s.exec_to_file(
                POSTGRES_POD,
                shared_namespace,
                ["pg_dump", "-U", POSTGRES_USER, "-Fc", "--no-owner", "--no-acl", "--dbname", name],
                destination,
                container=POSTGRES_CONTAINER,
                environ=environ,
            ),
            copy_bucket=lambda bucket, destination: _copy_bucket(s3_client(), bucket, destination),
            read_secret=lambda namespace, name: k8s.secret_with_type(name, namespace, environ=environ),
        )


def _copy_bucket(client: Any, bucket: str, destination: Path) -> dict[str, int]:
    """Download every object to `destination/<key>` and return {key: size}, each size checked against the listing."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    inventory: dict[str, int] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key, size = item["Key"], int(item["Size"])
            if key.endswith("/"):  # a folder marker, not an object
                continue
            target = (root / key).resolve()
            if not target.is_relative_to(root):
                raise BackupError(f"object key {key!r} in bucket {bucket} would be written outside the backup.")
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            if target.stat().st_size != size:
                raise BackupError(f"object {key} of bucket {bucket} is {target.stat().st_size} bytes, not {size}.")
            inventory[key] = size
    return inventory
