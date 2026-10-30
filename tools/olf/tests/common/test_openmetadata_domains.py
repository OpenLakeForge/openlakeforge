from __future__ import annotations

import pytest
from conftest import FakeOpenMetadata
from openlakeforge_services.openmetadata import (
    DataProduct,
    OpenMetadataClient,
    OpenMetadataError,
    OpenMetadataTransientError,
)


def _client(server: FakeOpenMetadata) -> OpenMetadataClient:
    return OpenMetadataClient(server.url, token=server.token)


def test_list_domains_follows_the_cursor_across_pages(fake_openmetadata: FakeOpenMetadata) -> None:
    fake_openmetadata.domains = [
        {"name": "sales", "displayName": "Sales"},
        {"name": "supply_chain", "displayName": "Supply Chain"},
        {"name": "finance"},
    ]

    domains = _client(fake_openmetadata).list_domains(page_size=2)

    assert domains == [("sales", "Sales"), ("supply_chain", "Supply Chain"), ("finance", "")]
    assert fake_openmetadata.requests == [("GET", "/api/v1/domains"), ("GET", "/api/v1/domains")]


def test_list_domains_with_none_is_empty(fake_openmetadata: FakeOpenMetadata) -> None:
    assert _client(fake_openmetadata).list_domains() == []


def test_list_domains_raises_on_a_rejected_request(fake_openmetadata: FakeOpenMetadata) -> None:
    fake_openmetadata.fail_status = 403

    with pytest.raises(OpenMetadataError, match="HTTP 403"):
        _client(fake_openmetadata).list_domains()


def test_list_domains_raises_a_transient_error_on_a_server_failure(fake_openmetadata: FakeOpenMetadata) -> None:
    fake_openmetadata.fail_status = 503

    with pytest.raises(OpenMetadataTransientError, match="HTTP 503"):
        _client(fake_openmetadata).list_domains()


def test_list_data_products_asks_for_and_returns_their_domains(fake_openmetadata: FakeOpenMetadata) -> None:
    fake_openmetadata.data_products = [
        {"name": "orders_mart", "displayName": "Orders mart", "domains": [{"name": "sales", "type": "domain"}]},
        {"name": "shared_kpis", "domains": [{"name": "sales"}, {"name": "finance"}]},
        {"name": "orphan"},
    ]

    products = _client(fake_openmetadata).list_data_products(page_size=2)

    assert products == [
        DataProduct(name="orders_mart", display_name="Orders mart", domains=("sales",)),
        DataProduct(name="shared_kpis", display_name="", domains=("sales", "finance")),
        DataProduct(name="orphan", display_name="", domains=()),
    ]
    assert fake_openmetadata.requests == [("GET", "/api/v1/dataProducts")] * 2
    assert all(query["fields"] == ["domains"] for query in fake_openmetadata.queries)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_carries_the_status_openmetadata_answered(fake_openmetadata: FakeOpenMetadata, status: int) -> None:
    fake_openmetadata.fail_status = status

    with pytest.raises(OpenMetadataError) as raised:
        _client(fake_openmetadata).list_data_products()

    assert raised.value.status == status


def test_an_unreachable_openmetadata_is_transient_with_no_status() -> None:
    with pytest.raises(OpenMetadataTransientError) as raised:
        OpenMetadataClient("http://127.0.0.1:9", token="t", timeout=2).list_data_products()

    assert raised.value.status is None
