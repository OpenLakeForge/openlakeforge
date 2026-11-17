"""`olf infra openmetadata quality-sync`: Slim is a strict no-op, Full publishes through the stage's own root."""

from __future__ import annotations

import contextlib
from contextlib import contextmanager

import pytest

from olf.infra.openmetadata._quality_sync import QualitySyncError, run_quality_sync

ENV = {
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_prod",
    "OPENLAKEFORGE_CATALOG_PROVIDER": "polaris",
    "OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://ops/activations/prod",
    "OPENLAKEFORGE_OPS_BUCKET_NAME": "ops",
    "OPENLAKEFORGE_GOVERNANCE_ENABLED": "true",
}


def _forbidden(*_args):
    raise AssertionError("a governance-disabled stage must not open the bucket or OpenMetadata")


def test_slim_makes_no_request_and_needs_no_credential():
    outcome = run_quality_sync(
        {**ENV, "OPENLAKEFORGE_GOVERNANCE_ENABLED": "false"}, open_store=_forbidden, open_catalog=_forbidden
    )
    assert outcome.governance_enabled is False
    assert outcome.report is None


def test_the_catalog_root_is_the_selected_stages_never_another():
    seen: list[str] = []

    class Client:
        def request(self, method, path, **kwargs):
            seen.append(path)
            raise AssertionError("no manifest, so no request")

    class Bucket:
        def list_objects_v2(self, **kwargs):
            seen.append(kwargs["Prefix"])
            return {"Contents": [], "IsTruncated": False}

        def put_object(self, **kwargs):
            seen.append(kwargs["Key"])

    @contextmanager
    def store(bucket):
        assert bucket == "ops"
        yield Bucket()

    @contextmanager
    def catalog():
        yield Client()

    outcome = run_quality_sync(ENV, open_store=store, open_catalog=catalog)
    assert outcome.stage == "prod"
    assert outcome.report is not None
    assert all("activations/prod/" in item for item in seen)


def test_the_batch_size_is_bounded():
    with pytest.raises(QualitySyncError):
        run_quality_sync(ENV, open_store=_forbidden, open_catalog=_forbidden, limit=10_000)


def test_a_non_stage_catalog_is_refused():
    with pytest.raises(QualitySyncError):
        run_quality_sync(
            {**ENV, "OPENLAKEFORGE_CATALOG_NAME": "polaris"}, open_store=_forbidden, open_catalog=_forbidden
        )



def test_a_malformed_schema_mapping_is_refused():
    with pytest.raises(QualitySyncError):
        run_quality_sync(
            {**ENV, "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": "[1]"},
            open_store=lambda bucket: contextlib.nullcontext(object()),
            open_catalog=lambda: contextlib.nullcontext(object()),  # type: ignore[arg-type,return-value]
        )
