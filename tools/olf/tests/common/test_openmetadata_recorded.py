"""The reconciler against responses OpenMetadata 1.13.6 actually returned.

`responses_1_13_6.json` was recorded from a live Full e2e run (see its `_source` key), so these tests catch a
reconciler that only agrees with a hand-written fake.
"""

import json
from pathlib import Path

import pytest
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_services.governance import MetadataConfig, MetadataReconciler
from openlakeforge_services.openmetadata import OpenMetadataClient
from openlakeforge_services.ownership import MARKER_PROPERTY, marker_of, marker_value, patch_operations

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "openmetadata" / "responses_1_13_6.json"
RECORDED = json.loads(_FIXTURE.read_text())


def _reconciler(monkeypatch: pytest.MonkeyPatch, request) -> MetadataReconciler:
    config = MetadataConfig(
        base_url="http://openmetadata",
        admin_email="",
        admin_password="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
        catalog_database_fqn="polaris.lakehouse_dev",
        catalog_silver_schema_fqns={},
        catalog_gold_schema_fqns={},
        storage_service="seaweedfs",
        storage_display_name="",
        storage_endpoint="",
        storage_region="",
        storage_bronze_bucket="",
        storage_silver_bucket="",
        storage_gold_bucket="",
    )
    client = OpenMetadataClient(config.base_url, token="t")
    monkeypatch.setattr(client, "request", request)
    return MetadataReconciler(config, client, revision="unknown")


def test_a_recorded_domain_that_matches_git_needs_no_patch() -> None:
    domain = RECORDED["domain_get"]
    desired = {
        "displayName": domain["displayName"],
        "description": domain["description"],
        "domainType": "Source-aligned",
        "extension": {MARKER_PROPERTY: marker_value("unknown")},
    }

    assert patch_operations(domain, desired) == []
    assert marker_of(domain) == "git:unknown"


def test_a_new_revision_patches_only_the_recorded_marker() -> None:
    operations = patch_operations(RECORDED["domain_get"], {"extension": {MARKER_PROPERTY: "git:capture"}})

    assert operations == [{"op": "replace", "path": "/extension/olfManaged", "value": "git:capture"}]
    assert RECORDED["domain_patch"]["extension"] == {MARKER_PROPERTY: "git:capture"}


def test_text_the_server_returns_html_escaped_is_not_a_difference() -> None:
    stored = {"description": "the run&#39;s &#34;manifest&#34;"}

    assert patch_operations(stored, {"description": "the run's \"manifest\""}) == []


def test_a_recorded_product_matches_when_its_fields_match() -> None:
    product = RECORDED["data_product_get"]

    owned = {"displayName": product["displayName"], "description": product["description"]}

    assert patch_operations(product, owned) == []


def test_held_assets_come_from_the_recorded_listing_including_non_table_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = RECORDED["data_product_assets"]
    reconciler = _reconciler(monkeypatch, lambda *_a, **_k: listing)

    held = reconciler.held_asset_ids("order_revenue")

    assert held == {asset["id"] for asset in listing["data"]}
    assert {asset["type"] for asset in listing["data"]} == {"table", "testCase"}


def test_the_marker_property_the_server_stored_is_recognised_as_already_defined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain_type = {"id": "type-1", "name": "domain", "customProperties": [RECORDED["custom_property_on_domain_type"]]}
    fake = FakeOpenMetadata()
    for entity_type in fake.types.values():
        entity_type["customProperties"] = [RECORDED["custom_property_on_domain_type"]]
    reconciler = _reconciler(monkeypatch, fake.request)

    reconciler.ensure_marker_properties()

    assert fake.writes == []
    assert domain_type["customProperties"][0]["name"] == MARKER_PROPERTY
