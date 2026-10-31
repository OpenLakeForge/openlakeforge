"""GET /api/v1/stages/{stage}/logs against an in-memory object store holding archives in the archiver's format."""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings, SettingsError

DEV_BASE = "s3://ops/activations/dev/logs"


class ObjectStore:
    """list_objects_v2 and get_object over a dict of keys, as boto3 answers them; `fail` raises instead."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.listed: list[str] = []
        self.fail: Callable[[], Exception] | None = None

    def archive(self, key: str, records: list[dict[str, Any]]) -> None:
        body = gzip.compress("".join(json.dumps(record) + "\n" for record in records).encode())
        self.objects[("ops", key)] = body

    def list_objects_v2(  # noqa: N803 - boto3's names
        self, *, Bucket: str, Prefix: str = "", ContinuationToken: str = ""
    ) -> dict[str, Any]:
        if self.fail:
            raise self.fail()
        self.listed.append(Prefix)
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def _line(pod: str, line: str, container: str = "main") -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v1",
        "archived_at": "2026-10-30T09:00:00+00:00",
        "namespace": "olf-dev",
        "pod": pod,
        "container": container,
        "line": line,
    }


KEY = "activations/dev/logs/k8s/namespace=olf-dev/date=2026-10-30/hour=09/k8s-logs-20261030T090000Z.ndjson.gz"


def _settings(settings: Settings, **extra: Any) -> Settings:
    raw = {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": list(settings.stages),
    }
    return Settings.from_mapping({**raw, **extra})


@pytest.fixture
def store() -> ObjectStore:
    fake = ObjectStore()
    fake.archive(KEY, [_line("run-1", "started"), _line("run-1", "done"), _line("run-2", "other")])
    return fake


@pytest.fixture
def api(settings: Settings, store: ObjectStore) -> TestClient:
    # prod is enabled but has no archive configured.
    configured = _settings(settings, log_archives={"dev": {"base_uri": DEV_BASE, "namespace": "olf-dev"}})
    return TestClient(create_app(configured, object_store=store))


def _as(mint: Mint, role: str) -> dict[str, str]:
    return bearer(mint(groups=list(ROLE_MAPPING[role])))


LOGS = "/api/v1/stages/dev/logs?date=2026-10-30"


@pytest.mark.parametrize(("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403)])
def test_logs_admit_data_engineer_and_above(api: TestClient, mint: Mint, role: str, status: int) -> None:
    assert api.get(LOGS, headers=_as(mint, role)).status_code == status


def test_a_stage_that_is_not_enabled_is_refused(api: TestClient, mint: Mint) -> None:
    response = api.get("/api/v1/stages/staging/logs?date=2026-10-30", headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert "not enabled" in response.json()["detail"]


def test_a_stage_with_no_archive_is_not_configured(api: TestClient, mint: Mint) -> None:
    response = api.get("/api/v1/stages/prod/logs?date=2026-10-30", headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert "no log archive" in response.json()["detail"]


def test_the_stage_reads_only_its_own_namespace_and_prefix(api: TestClient, store: ObjectStore, mint: Mint) -> None:
    response = api.get(LOGS + "&hour=9", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert store.listed == ["activations/dev/logs/k8s/namespace=olf-dev/date=2026-10-30/hour=09/"]


def test_lines_are_filtered_by_pod_and_limited(api: TestClient, mint: Mint) -> None:
    response = api.get(LOGS + "&pod=run-1&limit=1", headers=_as(mint, "data-engineer"))

    assert response.json() == {
        "lines": [
            {
                "archived_at": "2026-10-30T09:00:00+00:00",
                "namespace": "olf-dev",
                "pod": "run-1",
                "container": "main",
                "line": "started",
                "error": None,
            }
        ],
        "truncated": True,
    }


def test_a_window_with_everything_shown_is_not_truncated(api: TestClient, mint: Mint) -> None:
    body = api.get(LOGS, headers=_as(mint, "data-engineer")).json()

    assert [line["line"] for line in body["lines"]] == ["started", "done", "other"]
    assert body["truncated"] is False


@pytest.mark.parametrize("query", ["", "?date=yesterday", "?date=2026-10-30&hour=24", "?date=2026-10-30&limit=0"])
def test_a_missing_or_malformed_window_is_refused(api: TestClient, mint: Mint, query: str) -> None:
    assert api.get("/api/v1/stages/dev/logs" + query, headers=_as(mint, "data-engineer")).status_code == 422


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (
            lambda: ClientError({"Error": {"Code": "AccessDenied", "Message": "bucket ops"}}, "ListObjectsV2"),
            "the object store refused stage 'dev''s log archive",
        ),
        (lambda: EndpointConnectionError(endpoint_url="http://seaweedfs-s3:8333"), "the object store is unreachable"),
    ],
)
def test_an_object_store_failure_is_a_bad_gateway_that_names_no_bucket_or_endpoint(
    api: TestClient, store: ObjectStore, mint: Mint, failure: Callable[[], Exception], detail: str
) -> None:
    store.fail = failure

    response = api.get(LOGS, headers=_as(mint, "data-engineer"))

    assert response.status_code == 502
    assert response.json() == {"detail": detail}


def test_an_archive_that_is_not_gzip_is_a_bad_gateway(api: TestClient, store: ObjectStore, mint: Mint) -> None:
    store.objects[("ops", KEY)] = b"not gzip"

    response = api.get(LOGS, headers=_as(mint, "data-engineer"))

    assert response.status_code == 502
    assert response.json() == {"detail": "stage 'dev''s log archive is unreadable"}


def test_the_default_store_is_an_s3_client_on_the_configured_endpoint(settings: Settings) -> None:
    configured = _settings(settings, s3_endpoint_url="http://seaweedfs-s3.olf-system:8333", s3_region="eu-west-1")

    store = create_app(configured).state.object_store

    assert store.meta.endpoint_url == "http://seaweedfs-s3.olf-system:8333"
    assert store.meta.region_name == "eu-west-1"


@pytest.mark.parametrize(
    "archives",
    [
        {"staging": {"base_uri": DEV_BASE, "namespace": "olf-staging"}},
        {"dev": {"base_uri": "/var/log", "namespace": "olf-dev"}},
        {"dev": {"base_uri": DEV_BASE, "namespace": "olf-dev/date=2026-10-30"}},
        {"dev": {"base_uri": DEV_BASE}},
    ],
)
def test_a_log_archive_setting_that_could_not_be_served_refuses_to_load(
    settings: Settings, archives: dict[str, Any]
) -> None:
    with pytest.raises(SettingsError):
        _settings(settings, log_archives=archives)
