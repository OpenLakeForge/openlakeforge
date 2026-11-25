"""GET /api/v1/stages/{stage}/logs against an in-memory object store holding archives in the archiver's format."""

from __future__ import annotations

import gzip
import io
import json
import logging
from collections.abc import Callable
from datetime import datetime
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
        # LastModified of the objects given one; the others are listed without it.
        self.modified: dict[str, datetime] = {}
        self.listed: list[str] = []
        self.opened: list[str] = []
        self.fail: Callable[[], Exception] | None = None

    def archive(self, key: str, records: list[dict[str, Any]], modified: datetime | None = None) -> None:
        body = gzip.compress("".join(json.dumps(record) + "\n" for record in records).encode())
        self.objects[("ops", key)] = body
        if modified is not None:
            self.modified[key] = modified

    def list_objects_v2(  # noqa: N803 - boto3's names
        self, *, Bucket: str, Prefix: str = "", Delimiter: str = "", ContinuationToken: str = ""
    ) -> dict[str, Any]:
        if self.fail:
            raise self.fail()
        self.listed.append(Prefix)
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        nested = {key for key in keys if Delimiter and Delimiter in key.removeprefix(Prefix)}
        contents = [
            {"Key": key, **({"LastModified": self.modified[key]} if key in self.modified else {})}
            for key in keys
            if key not in nested
        ]
        prefixes = sorted({Prefix + key.removeprefix(Prefix).split(Delimiter)[0] + Delimiter for key in nested})
        return {"Contents": contents, "CommonPrefixes": [{"Prefix": p} for p in prefixes], "IsTruncated": False}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.opened.append(Key)
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
    assert set(store.listed) == {
        "activations/dev/logs/v2/namespace=olf-dev/date=2026-10-30/",
        "activations/dev/logs/k8s/namespace=olf-dev/date=2026-10-30/hour=09/",
    }


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
                "stage": None,
                "component": None,
                "domain": None,
                "product": None,
                "run_id": None,
                "stream": None,
                "missing": None,
                "source": "v1",
                # The archiver wrote this line without the kubelet's timestamp, and the archive time is not one.
                "timestamp": None,
                "level": None,
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


def test_the_default_store_reads_with_the_mounted_key_on_the_configured_endpoint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "olf0123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    configured = _settings(
        settings,
        log_archives={"dev": {"base_uri": DEV_BASE, "namespace": "olf-dev"}},
        s3_endpoint_url="http://seaweedfs-s3.olf-system:8333",
        s3_region="eu-west-1",
    )

    store = create_app(configured).state.object_store

    assert store.meta.endpoint_url == "http://seaweedfs-s3.olf-system:8333"
    assert store.meta.region_name == "eu-west-1"
    assert store._request_signer._credentials.access_key == "olf0123"


def test_without_a_mounted_key_the_logs_route_says_so_at_once(
    settings: Settings, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing to fall back on: boto3's chain would otherwise try the instance metadata endpoint.
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    configured = _settings(settings, log_archives={"dev": {"base_uri": DEV_BASE, "namespace": "olf-dev"}})

    response = TestClient(create_app(configured)).get(LOGS, headers=_as(mint, "data-engineer"))

    assert response.status_code == 503
    assert response.json() == {"detail": "the platform API holds no object-store key for the log archive"}


def test_no_store_is_built_without_a_log_archive(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "olf0123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    assert create_app(settings).state.object_store is None


def test_an_archive_record_missing_its_fields_is_a_bad_gateway(api: TestClient, store: ObjectStore, mint: Mint) -> None:
    store.archive(KEY, [{"schema": "openlakeforge.k8s_log.v1", "pod": "p"}])

    response = api.get(LOGS, headers=_as(mint, "data-engineer"))

    assert response.status_code == 502
    assert response.json() == {"detail": "stage 'dev''s log archive is unreadable"}


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


def _v2(time: str, message: str, namespace: str = "olf-dev", **fields: Any) -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v2",
        "time": time,
        "stage": "dev",
        "namespace": namespace,
        "pod": "dagster-run-8f1c2d4e-x7k2p",
        "container": "runner",
        "container_id": "3f9c",
        "stream": "stderr",
        "message": message,
        "missing": ["product"],
        "dedup_key": f"3f9c:{time}:{message}",
        **fields,
    }


V2_DEV = "activations/dev/logs/v2/namespace=olf-dev/date=2026-10-30/dev-4f1b.ndjson.gz"


def test_an_hour_of_the_day_lists_only_that_hour_partition(api: TestClient, store: ObjectStore, mint: Mint) -> None:
    day = "activations/dev/logs/v2/namespace=olf-dev/date=2026-10-30/"
    store.archive(f"{day}hour=09/dev-a.ndjson.gz", [_v2("2026-10-30T09:15:00Z", "INFO nine")])
    store.archive(f"{day}hour=10/dev-b.ndjson.gz", [_v2("2026-10-30T10:15:00Z", "INFO ten")])

    response = api.get(LOGS + "&hour=10", headers=_as(mint, "data-engineer"))

    assert [line["line"] for line in response.json()["lines"]] == ["INFO ten"]
    assert [prefix for prefix in store.listed if "/v2/" in prefix] == [day, f"{day}hour=10/"]


def test_v2_lines_are_found_by_run_without_the_pod_name_and_deduplicated(
    api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    replay = _v2("2026-10-30T09:00:02Z", "ERROR contract violated", run_id="r1", component="floe")
    store.archive(V2_DEV, [_v2("2026-10-30T09:00:01Z", "INFO other run", run_id="r2"), replay, replay])

    body = api.get(LOGS + "&run_id=r1&severity=error", headers=_as(mint, "data-engineer")).json()

    assert [(line["line"], line["run_id"], line["source"], line["missing"]) for line in body["lines"]] == [
        ("ERROR contract violated", "r1", "v2", ["product"])
    ]


@pytest.fixture
def two_stages(settings: Settings, store: ObjectStore) -> TestClient:
    store.archive(
        "activations/prod/logs/v2/namespace=olf-prod/date=2026-10-30/prod-1.ndjson.gz",
        [_v2("2026-10-30T09:00:00Z", "prod secret", namespace="olf-prod", stage="prod")],
    )
    configured = _settings(
        settings,
        log_archives={
            "dev": {"base_uri": DEV_BASE, "namespace": "olf-dev"},
            "prod": {"base_uri": "s3://ops/activations/prod/logs", "namespace": "olf-prod"},
        },
    )
    return TestClient(create_app(configured, object_store=store))


def test_a_dev_caller_cannot_read_prod_logs_and_prod_is_never_listed_for_them(
    two_stages: TestClient, store: ObjectStore, mint: Mint
) -> None:
    response = two_stages.get("/api/v1/stages/prod/logs?date=2026-10-30", headers=_as(mint, "data-engineer"))

    assert response.status_code == 403
    assert store.listed == []


def test_a_platform_admin_reads_prod_from_the_prod_prefix_only(
    two_stages: TestClient, store: ObjectStore, mint: Mint
) -> None:
    response = two_stages.get("/api/v1/stages/prod/logs?date=2026-10-30", headers=_as(mint, "platform-admin"))

    assert [line["line"] for line in response.json()["lines"]] == ["prod secret"]
    assert all(prefix.startswith("activations/prod/") for prefix in store.listed)


@pytest.mark.parametrize("query", ["&severity=loud", "&since=yesterday", "&until=x"])
def test_a_malformed_filter_is_refused(api: TestClient, mint: Mint, query: str) -> None:
    assert api.get(LOGS + query, headers=_as(mint, "data-engineer")).status_code == 422


def test_each_line_says_which_archive_layout_it_came_from(api: TestClient, store: ObjectStore, mint: Mint) -> None:
    store.archive(V2_DEV, [_v2("2026-10-30T08:00:00Z", "from the collector")])

    lines = api.get(LOGS + "&pod=dagster-run-8f1c2d4e-x7k2p", headers=_as(mint, "data-engineer")).json()["lines"]
    both = api.get(LOGS + "&limit=10", headers=_as(mint, "data-engineer")).json()["lines"]

    assert [line["source"] for line in lines] == ["v2"]
    assert {line["source"] for line in both} == {"v1", "v2"}


def test_each_line_carries_the_time_and_level_its_source_recorded_and_null_otherwise(
    api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    # A v1 object as the polling archiver has always written it: the kubelet timestamp opens the line.
    store.archive(
        KEY,
        [
            _line("run-1", "2026-10-30T09:00:00.123456789Z ERROR contract violated"),
            _line("run-1", "2026-10-30T09:00:01.000000001Z plain text"),
            {**_line("run-1", ""), "line": None, "archive_error": "container not found"},
        ],
    )
    store.archive(
        V2_DEV,
        [
            _v2("2026-10-30T09:00:02.5Z", '{"level": "warn", "msg": "slow"}'),
            _v2("2026-10-30T09:00:03Z", "Error occurred while executing"),
        ],
    )

    lines = api.get(LOGS + "&pod=run-1", headers=_as(mint, "data-engineer")).json()["lines"]
    lines += api.get(LOGS + "&pod=dagster-run-8f1c2d4e-x7k2p", headers=_as(mint, "data-engineer")).json()["lines"]

    assert [(line["source"], line["timestamp"], line["level"]) for line in lines] == [
        ("v1", None, None),
        ("v1", "2026-10-30T09:00:00.123456789Z", "error"),
        ("v1", "2026-10-30T09:00:01.000000001Z", None),
        ("v2", "2026-10-30T09:00:02.5Z", "warning"),
        # Prose that opens with the word is not a level.
        ("v2", "2026-10-30T09:00:03Z", None),
    ]


def test_the_severity_floor_reads_the_same_level_the_line_carries(
    api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    store.archive(
        V2_DEV,
        [
            _v2("2026-10-30T09:00:01Z", "2026-10-30 09:00:01 +0000 - dagster - ERROR - orders - failed"),
            _v2("2026-10-30T09:00:02Z", "retrying after error"),
        ],
    )

    body = api.get(LOGS + "&severity=error", headers=_as(mint, "data-engineer")).json()

    assert [(line["line"], line["level"]) for line in body["lines"]] == [
        ("2026-10-30 09:00:01 +0000 - dagster - ERROR - orders - failed", "error")
    ]


def test_q_keeps_the_lines_whose_message_holds_it_ignoring_case_before_the_limit(
    api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    store.archive(
        V2_DEV,
        [
            _v2("2026-10-30T09:00:01Z", "INFO Contract loaded"),
            _v2("2026-10-30T09:00:02Z", "retrying the contract"),
            _v2("2026-10-30T09:00:03Z", "ERROR contract violated"),
            _v2("2026-10-30T09:00:04Z", "ERROR disk full"),
        ],
    )

    both = api.get(LOGS + "&q=CONTRACT%20violated&severity=warning", headers=_as(mint, "data-engineer")).json()
    first = api.get(LOGS + "&q=contract&limit=2", headers=_as(mint, "data-engineer")).json()
    all_three = api.get(LOGS + "&q=Contract&limit=3", headers=_as(mint, "data-engineer")).json()

    assert [line["line"] for line in both["lines"]] == ["ERROR contract violated"]
    assert ([line["line"] for line in first["lines"]], first["truncated"]) == (
        ["INFO Contract loaded", "retrying the contract"],
        True,
    )
    # Six lines are in the day, three match: the limit counts matches, so nothing more is waiting.
    assert all_three["truncated"] is True
    assert api.get(LOGS + "&q=Contract&limit=4", headers=_as(mint, "data-engineer")).json()["truncated"] is False


@pytest.mark.parametrize("query", ["&q=", "&q=" + "x" * 201, "&q=a%00b", "&q=a%0Ab", "&q=a%09b", "&q=%7F"])
def test_a_search_text_that_is_empty_too_long_or_holds_a_control_character_is_refused(
    api: TestClient, store: ObjectStore, mint: Mint, query: str
) -> None:
    assert api.get(LOGS + query, headers=_as(mint, "data-engineer")).status_code == 422
    assert store.listed == []


def test_a_search_of_two_hundred_characters_with_pattern_syntax_is_read_as_plain_text(
    api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    store.archive(V2_DEV, [_v2("2026-10-30T09:00:01Z", "a [literal] (x)+ " + "y" * 183)])

    plain = api.get(LOGS + "&q=%5Bliteral%5D%20(x)%2B%20" + "y" * 183, headers=_as(mint, "data-engineer"))
    pattern = api.get(LOGS + "&q=.%2A", headers=_as(mint, "data-engineer"))

    assert [line["source"] for line in plain.json()["lines"]] == ["v2"]
    assert pattern.json()["lines"] == []


def test_the_search_text_is_not_logged(api: TestClient, store: ObjectStore, mint: Mint, caplog: Any) -> None:
    store.archive(V2_DEV, [_v2("2026-10-30T09:00:01Z", "customer 4711 failed")])

    with caplog.at_level(logging.INFO):
        body = api.get(LOGS + "&q=customer%204711", headers=_as(mint, "data-engineer")).json()

    assert [line["line"] for line in body["lines"]] == ["customer 4711 failed"]
    # httpx is the test's own client logging the URL it sent; the server side must not.
    server = [record.getMessage() for record in caplog.records if not record.name.startswith("httpx")]
    assert server
    assert not any("4711" in message for message in server)


PLATFORM_ARCHIVE = {"base_uri": "s3://ops/platform/logs", "namespace": "olf-system"}
PLATFORM_DAY = "platform/logs/v2/namespace=olf-system/date=2026-10-30/"
PLATFORM_LOGS = "/api/v1/platform/logs?date=2026-10-30"


def _platform_line(component: str, message: str) -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v2",
        "time": "2026-10-30T09:00:00.000000000Z",
        "stage": "platform",
        "namespace": "olf-system",
        "pod": f"{component}-0",
        "container": component,
        "component": component,
        "message": message,
        "dedup_key": f"{component}:{message}",
    }


@pytest.fixture
def platform_api(settings: Settings, store: ObjectStore) -> TestClient:
    store.archive(
        PLATFORM_DAY + "hour=09/platform-1.ndjson.gz",
        [_platform_line("polaris", "ERROR catalog down"), _platform_line("seaweedfs", "ERROR disk full")],
    )
    # No stage archive: the platform route needs the key and its own setting only.
    return TestClient(create_app(_settings(settings, platform_log_archive=PLATFORM_ARCHIVE), object_store=store))


@pytest.mark.parametrize(
    ("role", "status"), [("platform-admin", 200), ("data-engineer", 403), ("analyst", 403), ("viewer", 403)]
)
def test_platform_logs_admit_platform_admin_only(
    platform_api: TestClient, store: ObjectStore, mint: Mint, role: str, status: int
) -> None:
    response = platform_api.get(PLATFORM_LOGS, headers=_as(mint, role))

    assert response.status_code == status
    assert bool(store.opened) == (status == 200)


def test_platform_logs_read_only_the_platform_prefix_and_filter_by_component(
    platform_api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    response = platform_api.get(
        PLATFORM_LOGS + "&hour=9&component=seaweedfs&severity=error", headers=_as(mint, "platform-admin")
    )

    assert [line["line"] for line in response.json()["lines"]] == ["ERROR disk full"]
    assert response.json()["truncated"] is False
    assert store.opened == [PLATFORM_DAY + "hour=09/platform-1.ndjson.gz"]
    assert all(prefix.startswith("platform/logs/") for prefix in store.listed)


def test_platform_logs_with_no_platform_archive_are_not_configured(api: TestClient, mint: Mint) -> None:
    response = api.get(PLATFORM_LOGS, headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert response.json()["detail"] == "no platform log archive is configured"


def test_a_refused_platform_read_names_the_archive_and_nothing_of_the_store(
    platform_api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    store.fail = lambda: ClientError({"Error": {"Code": "AccessDenied", "Message": "ops"}}, "ListObjectsV2")

    response = platform_api.get(PLATFORM_LOGS, headers=_as(mint, "platform-admin"))

    assert response.status_code == 502
    assert response.json()["detail"] == "the object store refused the platform log archive"


@pytest.mark.parametrize("wrong", [{"base_uri": "ops/platform/logs"}, {"namespace": "a/b"}])
def test_a_malformed_platform_archive_refuses_to_start(settings: Settings, wrong: dict[str, str]) -> None:
    with pytest.raises(SettingsError, match="platform_log_archive"):
        _settings(settings, platform_log_archive={**PLATFORM_ARCHIVE, **wrong})
