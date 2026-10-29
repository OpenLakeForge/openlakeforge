from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Iterable

import boto3
import dlt
from botocore.client import Config


@dataclass(frozen=True)
class BronzeLoadResult:
    entity: str
    rows: int
    uri: str
    source_file: str


def load_entities_to_bronze(
    *,
    entities: tuple[str, ...],
    raw_dir: Path,
    bronze_prefix: str,
) -> dict[str, BronzeLoadResult]:
    return {
        entity: load_entity_to_bronze(
            entity=entity,
            raw_dir=raw_dir,
            bronze_prefix=bronze_prefix,
            allowed_entities=entities,
        )
        for entity in entities
    }


def load_entity_to_bronze(
    *,
    entity: str,
    raw_dir: Path,
    bronze_prefix: str,
    allowed_entities: tuple[str, ...],
) -> BronzeLoadResult:
    if entity not in allowed_entities:
        raise ValueError(f"unsupported entity for Bronze load: {entity}")

    source_file = raw_dir / f"{entity}.csv"
    rows = list(_dlt_csv_resource(entity, source_file))
    if not rows:
        raise ValueError(f"{source_file} did not produce any rows")

    bucket = _required_env("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET")
    key = f"{bronze_prefix}/{entity}/{entity}.csv"
    _put_csv(bucket=bucket, key=key, rows=rows)

    return BronzeLoadResult(
        entity=entity,
        rows=len(rows),
        uri=f"s3://{bucket}/{key}",
        source_file=str(source_file),
    )


def _dlt_csv_resource(entity: str, source_file: Path) -> Iterable[dict[str, str]]:
    @dlt.resource(name=entity, write_disposition="replace")
    def _rows():
        with source_file.open(newline="", encoding="utf-8") as handle:
            yield from csv.DictReader(handle)

    yield from _rows()


def _put_csv(*, bucket: str, key: str, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)

    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=buffer.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )


def _s3_client():
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL_S3")
    force_path_style = os.environ.get("AWS_S3_FORCE_PATH_STYLE", "true").lower() == "true"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        config=Config(s3={"addressing_style": "path" if force_path_style else "virtual"}),
    )


def _required_env(name: str, *, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value
