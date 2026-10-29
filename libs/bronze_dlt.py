"""A dlt pipeline that loads one source into the stage's own Bronze bucket.

For sources that are real dlt sources (an API, a database) rather than the
CSV fixtures `libs.bronze_csv` uploads. dlt keeps a pipeline's incremental
state in its destination, so state lives in the stage's own bucket under a
stage-named pipeline: a DEV run can never move PROD's cursor (#546). The
working directory is fresh on every run; the state is restored from the
bucket, not from a pod's disk.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from typing import Any

import dlt


def bronze_pipeline(
    source: str, *, environ: Mapping[str, str] | None = None, bucket_url: str | None = None
) -> Any:
    """The source's pipeline; files land at `<bucket>/<source>/<resource>/`, where Floe reads Bronze.

    `bucket_url` replaces the stage's bucket, for a run against another
    filesystem; the stage still names the pipeline and so its state.
    """
    env = os.environ if environ is None else environ

    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} is not set; the stage activation must provide it for Bronze loads.")
        return value

    stage = required("OPENLAKEFORGE_STAGE")
    destination = bucket_url or f"s3://{required('OPENLAKEFORGE_STORAGE_BRONZE_BUCKET')}"
    # Only what the stage sets: unset keys leave dlt on the pod's default AWS
    # credential chain, which is how a cloud workload identity is picked up.
    credentials = {
        field: value
        for field, name in (
            ("endpoint_url", "AWS_ENDPOINT_URL_S3"),
            ("region_name", "OPENLAKEFORGE_STORAGE_REGION"),
            ("aws_access_key_id", "AWS_ACCESS_KEY_ID"),
            ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"),
        )
        if (value := env.get(name, "").strip())
    }
    return dlt.pipeline(
        pipeline_name=f"{stage}__{source}",
        pipelines_dir=tempfile.mkdtemp(prefix=f"olf-dlt-{source}-"),
        destination=dlt.destinations.filesystem(
            bucket_url=destination, credentials=credentials if destination.startswith("s3://") else None
        ),
        dataset_name=source,
    )
