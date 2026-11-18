"""The quality reconciler's settings and one pass over a stage's manifests (#797, ADR 0007)."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult
from openlakeforge_core.quality_sync import OmStatus
from openlakeforge_services.openmetadata import OpenMetadataError, OpenMetadataTransientError

from openlakeforge_platform_api import quality_sync
from openlakeforge_platform_api.quality_sync import ReconcilerSettings, reconcile
from openlakeforge_platform_api.settings import SettingsError

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
TOKEN = "ingestion-bot-token-value"
ENVIRON = {
    "OPENLAKEFORGE_STAGE": "dev",
    "OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://ops/activations/dev/",
    "OPENMETADATA_URL": "http://openmetadata.olf-system:8585",
    "OPENLAKEFORGE_CATALOG_DATABASE_FQN": "polaris.lakehouse_dev",
    "AWS_ACCESS_KEY_ID": "key",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "OPENMETADATA_TOKEN": TOKEN,
}


class Bucket:
    """The calls of a boto3 S3 client the publisher makes, over a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.down = False

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if self.down:
            raise RuntimeError("bucket down")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        if self.down:
            raise RuntimeError("bucket down")
        self.objects[Key] = Body

    def list_objects_v2(  # noqa: N803
        self, *, Bucket: str, Prefix: str = "", ContinuationToken: str = ""
    ) -> dict[str, Any]:
        if self.down:
            raise RuntimeError("bucket down")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k, "ETag": "e", "LastModified": NOW} for k in keys], "IsTruncated": False}


class Catalog:
    """OpenMetadata with no table registered: the lookup is a 404 and nothing else is ever called."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.down = False

    def request(self, method: str, path: str, **_: Any) -> dict[str, Any]:
        self.calls.append((method, path))
        if self.down:
            raise OpenMetadataTransientError("connection refused")
        raise OpenMetadataError("not found", status=404)


def put_manifest(bucket: Bucket, *, stage: str = "dev") -> None:
    result = QualityResult(
        engine="floe",
        stage=stage,
        domain="sales",
        product="revenue",
        table="orders",
        check_id="not_null",
        engine_severity="warn",
        outcome=QualityOutcome.WARN,
        pipeline_outcome=PipelineOutcome.CONTINUED,
        observed_at=NOW,
        dagster_run_id="run-1",
    )
    manifest = QualityManifest(stage, "sales", "revenue", "run-1", (result,), complete=True)
    bucket.objects[f"activations/{stage}/{manifest.key}"] = manifest.to_json().encode()


def test_settings_default_to_a_bounded_batch_and_an_incident_free_warn() -> None:
    settings = ReconcilerSettings.from_environ(ENVIRON)

    assert (settings.stage, settings.limit, settings.warn_status) == ("dev", 25, OmStatus.ABORTED)
    assert settings.silver_schemas is None


@pytest.mark.parametrize(
    "override",
    [
        {"OPENLAKEFORGE_STAGE": "Dev"},
        # Another stage's prefix under this stage's name.
        {"OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://ops/activations/prod/"},
        {"OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://ops/"},
        {"OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://ops/activations/dev2/"},
        {"OPENLAKEFORGE_CATALOG_DATABASE_FQN": "polaris.lakehouse_prod"},
        {"OPENMETADATA_URL": "openmetadata:8585"},
        {"OPENLAKEFORGE_QUALITY_SYNC_LIMIT": "0"},
        {"OPENLAKEFORGE_QUALITY_SYNC_LIMIT": "201"},
        {"OPENLAKEFORGE_QUALITY_SYNC_LIMIT": "many"},
        {"OPENLAKEFORGE_QUALITY_WARN_STATUS": "Success"},
        {"OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": "[1]"},
    ],
)
def test_settings_that_could_reach_another_stage_or_widen_the_batch_are_refused(override: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        ReconcilerSettings.from_environ({**ENVIRON, **override})


def test_a_schema_of_another_stage_is_refused_before_any_request() -> None:
    settings = ReconcilerSettings.from_environ(
        {**ENVIRON, "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": '{"sales": "polaris.lakehouse_prod.sales_silver"}'}
    )
    catalog = Catalog()

    with pytest.raises(SettingsError):
        reconcile(settings, store=Bucket(), client=catalog)  # type: ignore[arg-type]

    assert catalog.calls == []


def test_an_unregistered_table_stays_pending_in_this_stages_summary_and_nowhere_else() -> None:
    bucket = Bucket()
    put_manifest(bucket)
    # Another stage's evidence in the same bucket is neither read nor touched.
    put_manifest(bucket, stage="prod")
    before = dict(bucket.objects)

    report = reconcile(ReconcilerSettings.from_environ(ENVIRON), store=bucket, client=Catalog())  # type: ignore[arg-type]

    assert (report.published, report.deferred) == (0, 1)
    written = {key for key in bucket.objects if bucket.objects[key] != before.get(key)}
    assert written == {
        "activations/dev/quality-sync/summary.json",
        "activations/dev/quality-sync/state/sales/revenue/run-1.json",
    }
    summary = json.loads(bucket.objects["activations/dev/quality-sync/summary.json"])
    assert (summary["stage"], summary["pending"], summary["failed"]) == ("dev", 1, 0)


def _run(monkeypatch: pytest.MonkeyPatch, bucket: Bucket, catalog: Catalog) -> int:
    monkeypatch.setattr(quality_sync, "_object_store", lambda *_: bucket)
    monkeypatch.setattr(quality_sync, "_catalog_client", lambda *_: catalog)
    return quality_sync.main(environ=ENVIRON)


def test_a_pass_with_work_waiting_for_registration_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = Bucket()
    put_manifest(bucket)

    assert _run(monkeypatch, bucket, Catalog()) == 0


def test_an_openmetadata_outage_fails_the_job_and_leaves_the_result_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = Bucket()
    put_manifest(bucket)
    catalog = Catalog()
    catalog.down = True

    assert _run(monkeypatch, bucket, catalog) == 1

    assert json.loads(bucket.objects["activations/dev/quality-sync/summary.json"])["pending"] == 1


def test_an_unreachable_bucket_fails_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = Bucket()
    bucket.down = True

    assert _run(monkeypatch, bucket, Catalog()) == 1


def test_a_missing_token_is_a_misconfiguration_that_makes_no_request(caplog: pytest.LogCaptureFixture) -> None:
    environ = {k: v for k, v in ENVIRON.items() if k != "OPENMETADATA_TOKEN"}
    caplog.set_level(logging.INFO)

    assert quality_sync.main(environ=environ) == 2
    assert "OPENMETADATA_TOKEN" in caplog.text


def test_no_credential_reaches_the_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    bucket = Bucket()
    put_manifest(bucket)
    caplog.set_level(logging.DEBUG)

    _run(monkeypatch, bucket, Catalog())

    assert TOKEN not in caplog.text
