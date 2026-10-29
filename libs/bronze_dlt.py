"""A dlt pipeline that loads one source into the stage's own Bronze bucket.

For sources that are real dlt sources (an API, a database) rather than the
CSV fixtures `libs.bronze_csv` uploads. dlt keeps a pipeline's incremental
state in its destination, so state lives in the stage's own bucket under a
stage-named pipeline: a DEV run can never move PROD's cursor (#546). The
working directory is fresh on every run; the state is restored from the
bucket, not from a pod's disk.
"""

from __future__ import annotations

import inspect
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import dlt
from openlakeforge_domain import load_source

from libs.bronze_csv import BronzeLoadResult

_LAKEHOUSE_CODE_ROOT = Path(__file__).resolve().parents[1] / "lakehouse_code"
# Floe merges every pending Bronze file of an entity in one MERGE and, when a
# key repeats, keeps its first row in URI order. A Silver run that catches up
# on several batches must keep the newest version of a row, so the newest
# batch has to sort first: file names start with a countdown from the load id.
# Drop this once Floe keeps the last occurrence instead.
_NEWEST_FIRST_FROM = 10**17
_LAYOUT = "{table_name}/{newest_first}.{load_id}.{file_id}.{ext}"


def _newest_first(schema_name: str, table_name: str, load_id: str, file_id: str, ext: str) -> str:
    return f"{_NEWEST_FIRST_FROM - int(float(load_id) * 1_000_000):017d}"


def _declared_cursor(resource: Any) -> str | None:
    wrapper = resource.incremental
    if wrapper is None:
        return None
    if wrapper.incremental is not None:
        return wrapper.incremental.cursor_path
    # dlt binds a cursor declared as the resource function's default argument,
    # its usual idiom, only when the resource runs; before that the signature
    # is the only place it is visible.
    argument = wrapper.get_incremental_arg(inspect.signature(resource._pipe.gen))
    return argument.default.cursor_path if argument is not None else None


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
            bucket_url=destination,
            credentials=credentials if destination.startswith("s3://") else None,
            layout=_LAYOUT,
            extra_placeholders={"newest_first": _newest_first},
        ),
        dataset_name=source,
    )


def load_dlt_source(
    source: str,
    dlt_source: Any,
    resources: Sequence[str],
    *,
    full_refresh: bool = False,
    environ: Mapping[str, str] | None = None,
    bucket_url: str | None = None,
    lakehouse_root: Path | None = None,
) -> dict[str, BronzeLoadResult]:
    """Load `resources` of `dlt_source` into Bronze as `source.yaml` declares them.

    A `full` resource replaces its Bronze files on every run. An `incremental`
    one appends the rows past its cursor's last value as new files; the
    connector declares the cursor with `dlt.sources.incremental`, on the column
    the descriptor names. `full_refresh` drops the resources' files and cursors
    first, so the run extracts everything again.

    This is the loader contract for a source with incremental resources:
    `load_<source>_entities_to_bronze(resources, *, full_refresh=False)`.
    """
    declared = load_source((lakehouse_root or _LAKEHOUSE_CODE_ROOT) / "bronze" / source / "source.yaml")
    selected = dlt_source.with_resources(*resources)
    for name in resources:
        load = declared.load_of(name)
        resource = selected.resources[name]
        if not load.incremental:
            resource.apply_hints(write_disposition="replace")
            continue
        cursor = _declared_cursor(resource)
        if cursor != load.cursor:
            raise RuntimeError(
                f"{source}/{name} is incremental on {load.cursor!r} in source.yaml, but the connector's "
                f"dlt.sources.incremental cursor is {cursor!r}"
            )
        resource.apply_hints(write_disposition="append", primary_key=list(load.primary_key))

    pipeline = bronze_pipeline(source, environ=environ, bucket_url=bucket_url)
    info = pipeline.run(selected, loader_file_format="parquet", refresh="drop_resources" if full_refresh else None)
    row_counts = pipeline.last_trace.last_normalize_info.row_counts
    destination = pipeline.destination_client().config.bucket_url.rstrip("/")
    load_id = info.loads_ids[-1] if info.loads_ids else "none"
    return {
        name: BronzeLoadResult(
            entity=name,
            rows=row_counts.get(name, 0),
            uri=f"{destination}/{source}/{name}/",
            source_file=f"{pipeline.pipeline_name} load {load_id}",
        )
        for name in resources
    }
