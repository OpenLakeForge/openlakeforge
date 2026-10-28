"""In-cluster object-storage client for the ops bucket, reached through kubectl port-forward."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from olf import k8s


@contextlib.contextmanager
def port_forward_client(
    bucket: str,
    *,
    service: str,
    remote_port: int,
    namespace: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
) -> Iterator[Any]:
    """Yield an S3 client connected to in-cluster object storage."""
    log_prefix = os.environ.get("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    log_path = f"{log_prefix}-seaweedfs-port-forward.log"
    with k8s.port_forward(service, remote_port, namespace, log_path=log_path) as local_port:
        endpoint = f"http://127.0.0.1:{local_port}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )
        _wait_for_bucket(client, bucket, endpoint)
        yield client


def _wait_for_bucket(client, bucket: str, endpoint: str, *, attempts: int = 60, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            client.head_bucket(Bucket=bucket)
            return
        except (ClientError, BotoCoreError):
            if attempt == attempts:
                raise RuntimeError(f"bucket '{bucket}' did not become available through {endpoint}.") from None
            time.sleep(delay)
