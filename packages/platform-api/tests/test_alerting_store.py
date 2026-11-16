"""The evaluator's settings and its objects in the ops bucket."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from _alerting_fakes import FakeBucket, settings_mapping
from openlakeforge_core.alerts import AlertState
from openlakeforge_core.alerts_state import state_to_json

from openlakeforge_platform_api.alerting.settings import EvaluatorSettings
from openlakeforge_platform_api.alerting.store import AlertStore, StoreError
from openlakeforge_platform_api.settings import SettingsError

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
BASE = "s3://ops/alerts/"


def test_a_missing_state_is_an_empty_one() -> None:
    assert AlertStore(FakeBucket(), BASE).read_state(NOW) == AlertState()


def test_a_state_round_trips_through_the_bucket() -> None:
    bucket = FakeBucket()
    state = AlertState(unknown_counts={"dev": 2}, sequences={"abc": 1})

    AlertStore(bucket, BASE).write_state(state)

    assert AlertStore(bucket, BASE).read_state(NOW) == state
    assert bucket.objects["alerts/state/v1/state.json"] == state_to_json(state).encode()


@pytest.mark.parametrize("body", [b"{not json", b'{"schema_version": 2}', b"\xff\xfe"])
def test_a_corrupt_state_is_copied_aside_and_read_as_empty(body: bytes) -> None:
    bucket = FakeBucket()
    bucket.objects["alerts/state/v1/state.json"] = body

    assert AlertStore(bucket, BASE).read_state(NOW) == AlertState()

    assert bucket.objects["alerts/state/v1/corrupt/20261116T120000Z.json"] == body
    # The original is left for the evaluator's next write to replace, never deleted here.
    assert bucket.objects["alerts/state/v1/state.json"] == body


def test_an_unreachable_store_is_a_store_error_not_an_empty_state() -> None:
    bucket = FakeBucket()
    bucket.down = True
    store = AlertStore(bucket, BASE)

    with pytest.raises(StoreError):
        store.read_state(NOW)
    with pytest.raises(StoreError):
        store.write_state(AlertState())
    with pytest.raises(StoreError):
        store.write_history(NOW, "x", {})


def test_history_is_partitioned_by_day_and_the_name_is_made_safe() -> None:
    bucket = FakeBucket()

    AlertStore(bucket, BASE).write_history(NOW, "inc-1-opened-delivery-pager/team", {"receiver": "pager/team"})

    assert bucket.under("alerts/history/v1/") == {
        "alerts/history/v1/date=2026-11-16/inc-1-opened-delivery-pager_team.json": {"receiver": "pager/team"}
    }


def test_a_state_prefix_replaces_the_base_prefix() -> None:
    bucket = FakeBucket()

    AlertStore(bucket, BASE, prefix="e2e/run-1/").write_state(AlertState())

    assert list(bucket.objects) == ["e2e/run-1/state/v1/state.json"]


def test_a_base_uri_must_be_an_s3_uri() -> None:
    with pytest.raises(ValueError, match="s3://"):
        AlertStore(FakeBucket(), "https://ops/alerts/")


def test_settings_route_a_receiver_by_its_stages() -> None:
    settings = EvaluatorSettings.from_mapping(
        {
            **settings_mapping(),
            "receivers": [
                {"name": "pager", "url_env": "PAGER_URL", "stages": ["dev"]},
                {"name": "all", "url_env": "X"},
            ],
        }
    )

    assert [(r.name, r.routes("dev"), r.routes("prod")) for r in settings.receivers] == [
        ("pager", True, False),
        ("all", True, True),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"state_base_uri": "s3://ops/alerts"},
        {"dagster_graphql_urls": {}},
        {"receivers": [{"name": "ops", "url_env": "X", "stages": ["prod"]}]},
        {"receivers": [{"name": "ops", "url_env": "X"}, {"name": "ops", "url_env": "Y"}]},
        {"links": {"dagster_scheme": "ftp"}},
        {"links": {"dagster_host_template": "dagster.example.test"}},
    ],
)
def test_settings_refuse_a_contradiction(change: dict[str, Any]) -> None:
    with pytest.raises(SettingsError):
        EvaluatorSettings.from_mapping({**settings_mapping(), **change})


def test_settings_missing_a_field_are_refused() -> None:
    with pytest.raises(SettingsError, match="incomplete"):
        EvaluatorSettings.from_mapping({"stages": ["dev"]})
