from pathlib import Path

import pytest
from conftest import FakeSuperset

from olf.clients.base import ServiceClientError
from olf.clients.superset import SupersetClient

DASHBOARD_UUID = "6f1c1d3e-8f0a-4c1e-9a77-2a8a3c3b5f10"


def _client(server: FakeSuperset) -> SupersetClient:
    return SupersetClient(server.url, username="admin", password="secret")


def test_dashboards_logs_in_then_lists(fake_superset: FakeSuperset) -> None:
    fake_superset.dashboards["orders"] = 7

    assert _client(fake_superset).dashboards() == [{"id": 7, "slug": "orders"}]


def test_login_waits_out_a_superset_that_is_still_starting(
    fake_superset: FakeSuperset, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    fake_superset.login_failures = 2

    client = _client(fake_superset)
    client.login()

    assert client.token == "fake-token"
    assert fake_superset.login_attempts == 3


def test_login_does_not_retry_rejected_credentials(fake_superset: FakeSuperset) -> None:
    with pytest.raises(ServiceClientError, match="HTTP 401"):
        SupersetClient(fake_superset.url, username="admin", password="wrong").login()

    assert fake_superset.login_attempts == 1


@pytest.mark.parametrize("dashboard", [DASHBOARD_UUID, "orders"])
def test_export_dashboard_selects_by_uuid_or_slug(fake_superset: FakeSuperset, dashboard: str) -> None:
    fake_superset.dashboards.update({DASHBOARD_UUID: 3, "orders": 3})
    fake_superset.exports[3] = b"PK-zip-bytes"
    client = _client(fake_superset)
    client.login()

    assert client.export_dashboard(dashboard) == b"PK-zip-bytes"


def test_export_dashboard_reports_an_unknown_dashboard(fake_superset: FakeSuperset) -> None:
    client = _client(fake_superset)
    client.login()

    with pytest.raises(ServiceClientError, match="HTTP 404"):
        client.export_dashboard("renamed-away")


def test_import_assets_uploads_the_bundle_with_the_csrf_token(fake_superset: FakeSuperset, tmp_path: Path) -> None:
    bundle = tmp_path / "orders_superset_bundle.zip"
    bundle.write_bytes(b"PK-bundle-bytes")
    client = _client(fake_superset)
    client.login()

    client.import_assets(bundle)

    assert fake_superset.imported == [b"PK-bundle-bytes"]
