from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.client import Config


def is_s3_uri(uri: str) -> bool:
    return uri.startswith("s3://")


def read_json_uri(uri: str) -> dict:
    value = json.loads(read_text_uri(uri))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document at {uri} must be an object")
    return value


def read_text_uri(uri: str) -> str:
    bucket, key = split_s3_uri(uri)
    payload = s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    return payload.decode("utf-8")


def upload_file(local_path: Path, *, bucket: str, key: str) -> str:
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    s3_client().upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return f"s3://{bucket}/{key}"


def upload_file_to_base_uri(local_path: Path, *, base_uri: str, relative_key: str) -> str:
    bucket, prefix = split_s3_uri(base_uri)
    key = "/".join(part.strip("/") for part in (prefix, relative_key) if part.strip("/"))
    return upload_file(local_path, bucket=bucket, key=key)


def split_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"expected s3:// URI, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def s3_client():
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        config=Config(s3={"addressing_style": "path"}),
    )
