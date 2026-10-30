from pathlib import Path
from typing import Any

import pytest
from libs import s3_artifacts


class _FakeS3:
    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None) -> None:
        self.objects = objects or {}
        self.uploads: list[tuple[str, str, str, dict[str, str]]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        body = self.objects[(Bucket, Key)]
        return {"Body": type("Body", (), {"read": lambda _self: body})()}

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict[str, str]) -> None:  # noqa: N803
        self.uploads.append((filename, bucket, key, ExtraArgs))


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> _FakeS3:
    fake = _FakeS3({("ops", "stages/dev.json"): b'{"stage": "dev"}', ("ops", "list.json"): b"[1]"})
    monkeypatch.setattr(s3_artifacts, "s3_client", lambda: fake)
    return fake


@pytest.mark.parametrize("uri", ["http://bucket/key", "bucket/key", "s3:///key"])
def test_split_s3_uri_rejects_anything_but_a_bucket_uri(uri: str) -> None:
    with pytest.raises(ValueError, match="expected s3:// URI"):
        s3_artifacts.split_s3_uri(uri)


def test_split_s3_uri_separates_bucket_and_key() -> None:
    assert s3_artifacts.split_s3_uri("s3://ops/a/b.json") == ("ops", "a/b.json")
    assert s3_artifacts.split_s3_uri("s3://ops") == ("ops", "")
    assert s3_artifacts.is_s3_uri("s3://ops") and not s3_artifacts.is_s3_uri("file:///ops")


def test_read_json_uri_returns_the_object_and_refuses_other_json(s3: _FakeS3) -> None:
    assert s3_artifacts.read_json_uri("s3://ops/stages/dev.json") == {"stage": "dev"}
    with pytest.raises(ValueError, match="must be an object"):
        s3_artifacts.read_json_uri("s3://ops/list.json")


def test_upload_file_to_base_uri_joins_prefix_and_key_without_doubled_slashes(s3: _FakeS3, tmp_path: Path) -> None:
    report = tmp_path / "run_results.json"
    report.write_text("{}")

    uri = s3_artifacts.upload_file_to_base_uri(
        report, base_uri="s3://ops/runs/", relative_key="/dbt/x/run_results.json"
    )

    assert uri == "s3://ops/runs/dbt/x/run_results.json"
    assert s3.uploads == [(str(report), "ops", "runs/dbt/x/run_results.json", {"ContentType": "application/json"})]


def test_upload_file_to_a_bare_bucket_uses_the_relative_key_alone(s3: _FakeS3, tmp_path: Path) -> None:
    blob = tmp_path / "blob.unknownext"
    blob.write_text("x")

    uri = s3_artifacts.upload_file_to_base_uri(blob, base_uri="s3://ops", relative_key="a/blob")

    assert uri == "s3://ops/a/blob"
    assert s3.uploads[0][3] == {"ContentType": "application/octet-stream"}


def test_s3_client_targets_the_configured_endpoint_with_path_addressing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://seaweedfs-s3:8333")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")

    client = s3_artifacts.s3_client()

    assert client.meta.endpoint_url == "http://seaweedfs-s3:8333"
    assert client.meta.region_name == "eu-west-1"
    assert client.meta.config.s3["addressing_style"] == "path"
