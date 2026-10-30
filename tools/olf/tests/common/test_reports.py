import io
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml
from conftest import FakeSuperset
from openlakeforge_services import reports
from openlakeforge_services.superset import SupersetClient

_REPORT_DIR = "lakehouse_code/dashboards/superset/demo"
_DATABASE_UUID = "11111111-1111-5111-8111-111111111111"


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def step(self, message: str) -> None:
        self.messages.append(("step", message))

    def info(self, message: str) -> None:
        self.messages.append(("info", message))


def _client(server: FakeSuperset) -> SupersetClient:
    client = SupersetClient(server.url, username="admin", password="secret")
    client.login()
    return client


def _write_bundle(root: Path) -> None:
    bundle = root / _REPORT_DIR
    for kind in ("databases", "datasets", "dashboards"):
        (bundle / kind).mkdir(parents=True)
    (bundle / "metadata.yaml").write_text("version: 1.0.0\ntype: assets\n")
    (bundle / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: {reports.STAGE_NEUTRAL_SQLALCHEMY_URI}\nuuid: {_DATABASE_UUID}\n"
    )
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\nschema: order_revenue_gold\ndatabase_uuid: {_DATABASE_UUID}\n"
    )
    (bundle / "dashboards" / "dash.yaml").write_text("dashboard_title: Orders\n")


def test_import_reports_imports_each_bundle_and_grants_its_schemas(
    tmp_path: Path, fake_superset: FakeSuperset
) -> None:
    _write_bundle(tmp_path)
    fake_superset.permissions[1] = ("schema_access", "[Trino].[lakehouse_dev_order_revenue_gold]")
    progress = _Recorder()

    reports.import_reports(
        _client(fake_superset),
        tmp_path,
        [_REPORT_DIR],
        work_dir=tmp_path / "work",
        sqlalchemy_uri="trino://olf-dev@trino:8080/lakehouse_dev",
        schema_prefix="lakehouse_dev_",
        progress=progress,
    )

    (imported,) = fake_superset.imported
    root = reports.bundle_identity(_REPORT_DIR).root
    with ZipFile(io.BytesIO(imported)) as bundle:
        assert yaml.safe_load(bundle.read(f"{root}/datasets/mart.yaml"))["schema"] == "lakehouse_dev_order_revenue_gold"
    assert fake_superset.roles[reports.DATA_ACCESS_ROLE] == {1}
    assert progress.messages == [
        ("step", f"Importing Superset report assets from {tmp_path / 'work' / f'{root}.zip'}"),
        ("info", f"Deployed Superset report assets from {_REPORT_DIR}"),
    ]


def test_import_reports_says_so_when_superset_has_no_data_access_role(
    tmp_path: Path, fake_superset: FakeSuperset
) -> None:
    _write_bundle(tmp_path)
    fake_superset.roles.clear()
    progress = _Recorder()

    reports.import_reports(
        _client(fake_superset),
        tmp_path,
        [_REPORT_DIR],
        work_dir=tmp_path / "work",
        sqlalchemy_uri="trino://olf-dev@trino:8080/lakehouse_dev",
        progress=progress,
    )

    assert progress.messages[-1] == ("info", "Superset sign-in is off; no data-access role to grant")


def test_import_reports_refuses_a_directory_that_is_not_a_bundle(tmp_path: Path, fake_superset: FakeSuperset) -> None:
    (tmp_path / _REPORT_DIR).mkdir(parents=True)

    with pytest.raises(RuntimeError, match=f"missing Superset report metadata at {_REPORT_DIR}/metadata.yaml"):
        reports.import_reports(
            _client(fake_superset),
            tmp_path,
            [_REPORT_DIR],
            work_dir=tmp_path / "work",
            sqlalchemy_uri="trino://olf-dev@trino:8080/lakehouse_dev",
        )
    assert fake_superset.imported == []


def test_export_report_leaves_a_stage_neutral_assets_bundle(tmp_path: Path, fake_superset: FakeSuperset) -> None:
    export = io.BytesIO()
    with ZipFile(export, "w") as bundle:
        bundle.writestr("dashboard_export/metadata.yaml", "version: 1.0.0\ntype: Dashboard\n")
        bundle.writestr(
            "dashboard_export/databases/trino.yaml",
            yaml.safe_dump(
                {
                    "database_name": "Trino",
                    "sqlalchemy_uri": "trino://olf-prod@trino:8443/lakehouse_prod",
                    "uuid": _DATABASE_UUID,
                    "extra": reports.TLS_EXTRA,
                }
            ),
        )
        bundle.writestr(
            "dashboard_export/datasets/mart.yaml",
            "table_name: mart_orders\nschema: lakehouse_prod_order_revenue_gold\n",
        )
    fake_superset.dashboards["orders"] = 7
    fake_superset.exports[7] = export.getvalue()
    target = tmp_path / _REPORT_DIR
    progress = _Recorder()

    reports.export_report(
        _client(fake_superset),
        "orders",
        bundle_path=tmp_path / "work" / "orders.zip",
        target_dir=target,
        schema_prefix="lakehouse_prod_",
        progress=progress,
    )

    assert yaml.safe_load((target / "metadata.yaml").read_text()) == {"version": "1.0.0", "type": "assets"}
    database = yaml.safe_load((target / "databases" / "trino.yaml").read_text())
    assert database["sqlalchemy_uri"] == reports.STAGE_NEUTRAL_SQLALCHEMY_URI
    assert database["extra"] == {"engine_params": {}}
    assert yaml.safe_load((target / "datasets" / "mart.yaml").read_text())["schema"] == "order_revenue_gold"
    assert progress.messages == [("step", "Exporting dashboard 'orders' from Superset")]
