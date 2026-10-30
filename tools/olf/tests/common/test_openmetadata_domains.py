from __future__ import annotations

import pytest
from conftest import FakeOpenMetadata
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError, OpenMetadataTransientError


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
