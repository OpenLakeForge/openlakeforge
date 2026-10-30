from pathlib import Path

import pytest
from conftest import FakeSuperset
from openlakeforge_services.base import ServiceClientError

from olf.common.clients.superset import SupersetClient

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


def test_grant_permissions_fails_when_the_schema_was_not_imported(fake_superset: FakeSuperset) -> None:
    client = _client(fake_superset)
    client.login()

    with pytest.raises(ServiceClientError, match="was it imported"):
        client.grant_permissions("olf_data_access", "schema_access", ["[Trino].[missing]"])


def test_grant_permissions_skips_a_role_superset_never_created(fake_superset: FakeSuperset) -> None:
    client = _client(fake_superset)
    client.login()

    assert client.grant_permissions("nope", "schema_access", ["[Trino].[x]"]) is False


def test_grant_permissions_finds_a_permission_beyond_the_first_page(fake_superset: FakeSuperset) -> None:
    fake_superset.permissions.update({i: ("can_read", f"View{i}") for i in range(1, 250)})
    fake_superset.permissions[777] = ("schema_access", "[Trino].[late_gold]")
    client = _client(fake_superset)
    client.login()

    assert client.grant_permissions("olf_data_access", "schema_access", ["[Trino].[late_gold]"]) is True
    assert fake_superset.roles["olf_data_access"] == {777}


def test_import_assets_sends_the_database_password_as_a_form_field_not_in_the_zip(
    fake_superset: FakeSuperset, tmp_path: Path
) -> None:
    import json
    from zipfile import ZipFile

    bundle = tmp_path / "bundle.zip"
    with ZipFile(bundle, "w") as archive:
        archive.writestr("root/databases/trino.yaml", "sqlalchemy_uri: trino://u@h:8443/c")
        archive.writestr("root/datasets/d.yaml", "schema: s")
    client = _client(fake_superset)
    client.login()
    sent: dict = {}
    client.send = lambda method, path, **kwargs: sent.update(kwargs)  # type: ignore[method-assign]
    client._csrf_token = lambda: "t"  # type: ignore[method-assign]

    client.import_assets(bundle, database_password="pw")

    assert json.loads(sent["data"]["passwords"]) == {"databases/trino.yaml": "pw"}
    assert b"pw" not in bundle.read_bytes()
