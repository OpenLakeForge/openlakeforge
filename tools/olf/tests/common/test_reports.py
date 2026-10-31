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


def test_import_reports_imports_each_bundle_and_grants_its_schemas(tmp_path: Path, fake_superset: FakeSuperset) -> None:
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


_DASHBOARD_UUID = "22222222-2222-5222-8222-222222222222"


def _platform_api_superset(server: FakeSuperset) -> None:
    """A Superset whose init created the platform API's role and user, with one imported dashboard."""
    server.roles[reports.PLATFORM_API_ROLE] = set()
    server.users[reports.PLATFORM_API_USERNAME] = 9
    server.dashboards[_DASHBOARD_UUID] = 4
    server.dashboard_assets[4] = {"charts": [5], "datasets": [6]}
    # admin (1) imported everything; a person (2) co-owns the chart; the dataset was already handed over.
    server.owners.update({("dashboard", 4): [1], ("chart", 5): [1, 2], ("dataset", 6): [1, 9]})


def test_import_reports_grants_the_platform_api_its_schemas_and_hands_each_dashboard_over(
    tmp_path: Path, fake_superset: FakeSuperset
) -> None:
    _write_bundle(tmp_path)
    (tmp_path / _REPORT_DIR / "dashboards" / "dash.yaml").write_text(
        f"dashboard_title: Orders\nuuid: {_DASHBOARD_UUID}\n"
    )
    fake_superset.permissions[1] = ("schema_access", "[Trino].[order_revenue_gold]")
    _platform_api_superset(fake_superset)
    progress = _Recorder()

    reports.import_reports(
        _client(fake_superset),
        tmp_path,
        [_REPORT_DIR],
        work_dir=tmp_path / "work",
        sqlalchemy_uri="trino://olf-dev@trino:8080/lakehouse_dev",
        progress=progress,
    )

    assert fake_superset.roles[reports.PLATFORM_API_ROLE] == {1}
    assert fake_superset.roles[reports.DATA_ACCESS_ROLE] == {1}
    # Every owner is kept; the dataset that already had the user is left alone.
    assert fake_superset.owner_puts == [("dashboard", 4, [1, 9]), ("chart", 5, [1, 2, 9])]
    assert progress.messages[-1] == ("info", "Made olf-platform-api a co-owner of 2 report asset(s)")


def test_a_second_import_hands_nothing_over_again(tmp_path: Path, fake_superset: FakeSuperset) -> None:
    _platform_api_superset(fake_superset)
    client = _client(fake_superset)

    assert client.add_owner(_DASHBOARD_UUID, 9) == 2
    assert client.add_owner(_DASHBOARD_UUID, 9) == 0
    assert len(fake_superset.owner_puts) == 2


def test_without_the_platform_api_user_nothing_is_handed_over(tmp_path: Path, fake_superset: FakeSuperset) -> None:
    _write_bundle(tmp_path)
    (tmp_path / _REPORT_DIR / "dashboards" / "dash.yaml").write_text(
        f"dashboard_title: Orders\nuuid: {_DASHBOARD_UUID}\n"
    )
    fake_superset.dashboards[_DASHBOARD_UUID] = 4
    fake_superset.permissions[1] = ("schema_access", "[Trino].[order_revenue_gold]")
    progress = _Recorder()

    reports.import_reports(
        _client(fake_superset),
        tmp_path,
        [_REPORT_DIR],
        work_dir=tmp_path / "work",
        sqlalchemy_uri="trino://olf-dev@trino:8080/lakehouse_dev",
        progress=progress,
    )

    assert fake_superset.owner_puts == []
    assert progress.messages[-1] == ("info", "Superset has no olf-platform-api user; no report asset is handed over")


def test_terraform_creates_the_role_and_user_the_handover_names() -> None:
    terraform = Path(__file__).resolve().parents[4] / "infra/terraform/modules"
    superset = (terraform / "analytics/superset/main.tf").read_text()
    platform_api = (terraform / "access/platform-api/superset_users.tf").read_text()

    assert f'platform_api_role = "{reports.PLATFORM_API_ROLE}"' in superset
    assert f'superset_username     = "{reports.PLATFORM_API_USERNAME}"' in platform_api


def _upload(files: dict[str, str]) -> bytes:
    raw = io.BytesIO()
    with ZipFile(raw, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return raw.getvalue()


_UPLOAD = {
    "metadata.yaml": "version: 1.0.0\ntype: Dashboard\n",
    "databases/trino.yaml": f"database_name: Elsewhere\nsqlalchemy_uri: trino://x@evil:443/c\nuuid: {_DATABASE_UUID}\n",
    "datasets/mart.yaml": f"table_name: mart_orders\nschema: order_revenue_gold\ndatabase_uuid: {_DATABASE_UUID}\n",
    "charts/c.yaml": "slice_name: Revenue\n",
    "dashboards/d.yaml": f"dashboard_title: Orders\nuuid: {_DASHBOARD_UUID}\n",
    "notes.txt": "ignored",
}


def test_an_upload_loses_its_databases_and_gets_the_stages_schemas_and_a_sparse_bundle() -> None:
    prepared = reports.prepare_upload(_upload(_UPLOAD), {_DATABASE_UUID: "Trino"}, schema_prefix="lakehouse_dev_")

    with ZipFile(io.BytesIO(prepared.bundle)) as bundle:
        names = sorted(bundle.namelist())
        assert names == [
            "report/charts/c.yaml",
            "report/dashboards/d.yaml",
            "report/datasets/mart.yaml",
            "report/metadata.yaml",
        ]
        assert yaml.safe_load(bundle.read("report/metadata.yaml"))["type"] == "assets"
        assert yaml.safe_load(bundle.read("report/datasets/mart.yaml"))["schema"] == "lakehouse_dev_order_revenue_gold"
    assert prepared.dashboards == (_DASHBOARD_UUID,)
    assert prepared.schema_perms == {"[Trino].[lakehouse_dev_order_revenue_gold]"}


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"../datasets/x.yaml": "a: 1"}, "is not a path inside the report"),
        ({"/datasets/x.yaml": "a: 1"}, "is not a path inside the report"),
        ({"datasets/x.yaml": "table_name: t\nschema: s\ndatabase_uuid: 33333333-3333-5333-8333-333333333333\n"},
         "uses a database this stage's Superset does not have"),
        ({"datasets/x.yaml": f"table_name: t\ndatabase_uuid: {_DATABASE_UUID}\n"}, "has no schema"),
        ({"dashboards/d.yaml": "- not a mapping\n"}, "is not a YAML mapping"),
        ({"charts/c.yaml": "slice_name: x\n"}, "has no dashboard"),
    ],
)  # fmt: skip
def test_an_upload_the_api_must_not_import_is_refused_with_its_reason(files: dict[str, str], message: str) -> None:
    with pytest.raises(reports.UploadRejected, match=message):
        reports.prepare_upload(_upload(files), {_DATABASE_UUID: "Trino"})


def test_an_upload_that_is_not_a_zip_is_refused() -> None:
    with pytest.raises(reports.UploadRejected, match="not a zip archive"):
        reports.prepare_upload(b"not a zip", {})


def test_an_export_is_put_back_to_its_stage_neutral_form_in_memory() -> None:
    raw = _upload(
        {
            "dashboard_export/metadata.yaml": "version: 1.0.0\ntype: Dashboard\n",
            "dashboard_export/databases/trino.yaml": "database_name: Trino\nsqlalchemy_uri: trino://olf-dev@trino:8080/x\n",
            "dashboard_export/datasets/mart.yaml": "table_name: m\nschema: lakehouse_dev_order_revenue_gold\n",
        }
    )

    with ZipFile(io.BytesIO(reports.neutral_export(raw, schema_prefix="lakehouse_dev_"))) as bundle:
        assert yaml.safe_load(bundle.read("dashboard_export/metadata.yaml"))["type"] == "assets"
        database = yaml.safe_load(bundle.read("dashboard_export/databases/trino.yaml"))
        assert database["sqlalchemy_uri"] == reports.STAGE_NEUTRAL_SQLALCHEMY_URI
        assert yaml.safe_load(bundle.read("dashboard_export/datasets/mart.yaml"))["schema"] == "order_revenue_gold"


def test_the_client_imports_sparsely_lists_databases_and_reads_its_own_schema_access(
    tmp_path: Path, fake_superset: FakeSuperset
) -> None:
    fake_superset.databases = {_DATABASE_UUID: "Trino"}
    fake_superset.my_roles = {
        "olf_platform_api": [["can_import_", "ImportExportRestApi"], ["schema_access", "[Trino].[order_revenue_gold]"]]
    }
    bundle = tmp_path / "b.zip"
    bundle.write_bytes(reports.prepare_upload(_upload(_UPLOAD), {_DATABASE_UUID: "Trino"}).bundle)
    client = _client(fake_superset)

    client.import_assets(bundle, sparse=True)

    assert fake_superset.import_forms == [{"sparse": "true"}]
    assert client.databases() == {_DATABASE_UUID: "Trino"}
    assert client.my_schema_access() == {"[Trino].[order_revenue_gold]"}
