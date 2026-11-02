from pathlib import Path

import pytest
from libs import bronze_csv


class _FakeS3:
    def __init__(self) -> None:
        self.uploads: list[dict] = []

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: dict | None = None) -> None:  # noqa: N803
        # Snapshot the bytes now: the real client streams the file at call time.
        self.uploads.append(
            {"Bucket": Bucket, "Key": Key, "Body": Path(Filename).read_bytes(), "ExtraArgs": ExtraArgs or {}}
        )

    def put_object(self, **kwargs: object) -> None:
        raise AssertionError("the whole file body must not be handed to put_object")


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> _FakeS3:
    fake = _FakeS3()
    monkeypatch.setattr(bronze_csv, "_s3_client", lambda: fake)
    monkeypatch.setenv("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET", "lakehouse-dev-bronze")
    return fake


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    (tmp_path / "orders.csv").write_text("id,total\n1,9.5\n2,3\n", encoding="utf-8")
    (tmp_path / "customers.csv").write_text("id,name\n7,Ada\n", encoding="utf-8")
    (tmp_path / "empty.csv").write_text("id,total\n", encoding="utf-8")
    return tmp_path


def test_load_entities_to_bronze_uploads_each_csv_under_the_bronze_prefix(s3: _FakeS3, raw_dir: Path) -> None:
    results = bronze_csv.load_entities_to_bronze(
        entities=("orders", "customers"), raw_dir=raw_dir, bronze_prefix="shop/raw"
    )

    assert {name: (r.rows, r.uri) for name, r in results.items()} == {
        "orders": (2, "s3://lakehouse-dev-bronze/shop/raw/orders/orders.csv"),
        "customers": (1, "s3://lakehouse-dev-bronze/shop/raw/customers/customers.csv"),
    }
    uploaded = {put["Key"]: put for put in s3.uploads}
    assert uploaded["shop/raw/orders/orders.csv"]["Body"] == b"id,total\n1,9.5\n2,3\n"
    assert uploaded["shop/raw/orders/orders.csv"]["Bucket"] == "lakehouse-dev-bronze"
    assert uploaded["shop/raw/orders/orders.csv"]["ExtraArgs"]["ContentType"] == "text/csv"
    assert results["orders"].source_file == str(raw_dir / "orders.csv")


def test_the_uploaded_object_is_the_source_file_byte_for_byte(s3: _FakeS3, tmp_path: Path) -> None:
    body = b'id,note\r\n1,"line one\nline two"\r\n2,caf\xc3\xa9\r\n'
    (tmp_path / "notes.csv").write_bytes(body)

    result = bronze_csv.load_entity_to_bronze(
        entity="notes", raw_dir=tmp_path, bronze_prefix="p", allowed_entities=("notes",)
    )

    assert [u["Body"] for u in s3.uploads] == [body]
    assert result.rows == 2


def test_blank_lines_are_not_rows(s3: _FakeS3, tmp_path: Path) -> None:
    (tmp_path / "gaps.csv").write_text("id\n1\n\n2\n\n", encoding="utf-8")

    result = bronze_csv.load_entity_to_bronze(
        entity="gaps", raw_dir=tmp_path, bronze_prefix="p", allowed_entities=("gaps",)
    )

    assert result.rows == 2


def test_an_entity_outside_the_allowed_set_is_refused_before_any_upload(s3: _FakeS3, raw_dir: Path) -> None:
    with pytest.raises(ValueError, match="unsupported entity for Bronze load: customers"):
        bronze_csv.load_entity_to_bronze(
            entity="customers", raw_dir=raw_dir, bronze_prefix="p", allowed_entities=("orders",)
        )

    assert s3.uploads == []


def test_a_csv_without_rows_is_an_error_not_an_empty_upload(s3: _FakeS3, raw_dir: Path) -> None:
    with pytest.raises(ValueError, match="did not produce any rows"):
        bronze_csv.load_entity_to_bronze(
            entity="empty", raw_dir=raw_dir, bronze_prefix="p", allowed_entities=("empty",)
        )

    assert s3.uploads == []


def test_a_stage_without_a_bronze_bucket_fails_naming_the_variable(
    s3: _FakeS3, raw_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET")

    with pytest.raises(RuntimeError, match="OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"):
        bronze_csv.load_entity_to_bronze(
            entity="orders", raw_dir=raw_dir, bronze_prefix="p", allowed_entities=("orders",)
        )


def test_s3_client_honours_the_endpoint_and_addressing_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://seaweedfs-s3:8333")
    monkeypatch.setenv("AWS_S3_FORCE_PATH_STYLE", "false")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")

    client = bronze_csv._s3_client()

    assert client.meta.endpoint_url == "http://seaweedfs-s3:8333"
    assert client.meta.config.s3["addressing_style"] == "virtual"
