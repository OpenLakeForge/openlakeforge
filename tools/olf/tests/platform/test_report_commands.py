"""`olf report import|export` against a fake platform API: what is zipped and sent, and what is written back."""

from __future__ import annotations

import io
from pathlib import Path
from zipfile import ZipFile

import pytest
import test_reads
from openlakeforge_services import reports
from test_reads import ANSWERS, POST_ANSWERS, TOKEN, FakeAPI, _olf

from olf.platform import session as sessions
from olf.platform import writes

fake = test_reads.fake  # the fake platform API fixture
DASHBOARD = "dashboards/Orders.yaml"
DATASET = "datasets/trino/orders.yaml"


def _report(root: Path) -> Path:
    for name, text in {
        "metadata.yaml": "version: 1.0.0\ntype: assets\n",
        DASHBOARD: "dashboard_title: Orders\nslug: orders\n",
        DATASET: "table_name: orders\nschema: sales\n",
        "charts/Orders_by_day.yml": "slice_name: Orders by day\n",
        "databases/trino.yaml": "database_name: trino\nsqlalchemy_uri: trino://secret@trino\n",
        "dashboards/.Orders.yaml.swp": "editor junk",
        "README.md": "notes",
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def _bundle() -> bytes:
    out = io.BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("report/metadata.yaml", "version: 1.0.0\ntype: assets\n")
        archive.writestr(f"report/{DASHBOARD}", "dashboard_title: Orders\n")
    return out.getvalue()


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        POST_ANSWERS, "/api/v1/stages/dev/reports", {"dashboards": ["d1"], "stage": "dev", "request_id": "0" * 32}
    )
    monkeypatch.setitem(ANSWERS, "/api/v1/stages/dev/reports/orders%20v2/export", _bundle())


def test_import_sends_metadata_and_the_assets_the_api_imports_and_never_the_databases(
    fake: FakeAPI, answers: None, tmp_path: Path
) -> None:
    result = _olf("report", "import", str(_report(tmp_path / "orders")))

    assert result.exit_code == 0, result.output
    assert result.stdout == "Imported 1 dashboard(s) into dev's Superset.\n"
    [request] = fake.requests
    assert request["method"] == "POST" and request["path"] == "/api/v1/stages/dev/reports"
    assert request["authorization"] == f"Bearer {TOKEN}"
    with ZipFile(io.BytesIO(request["body"])) as archive:
        assert sorted(archive.namelist()) == sorted(["metadata.yaml", "charts/Orders_by_day.yml", DASHBOARD, DATASET])


def test_import_refuses_a_directory_without_metadata_before_sending_anything(fake: FakeAPI, tmp_path: Path) -> None:
    result = _olf("report", "import", str(tmp_path))

    assert result.exit_code != 0
    assert "has no metadata.yaml; it is not a report directory" in result.stderr
    assert fake.requests == []


@pytest.mark.parametrize(
    ("limit", "value", "match"),
    [
        ("MAX_UPLOAD_MEMBERS", 2, "more than 2 YAML files"),
        ("MAX_UPLOAD_MEMBER_BYTES", 30, "is over 30 bytes"),
        ("MAX_UPLOAD_TOTAL_BYTES", 100, "over 100 bytes of YAML"),
    ],
)
def test_a_report_over_the_apis_limits_is_refused_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: str, value: int, match: str
) -> None:
    monkeypatch.setattr(reports, limit, value)
    with pytest.raises(sessions.PlatformError, match=match):
        writes.zip_report(_report(tmp_path))


def test_a_report_that_zips_over_the_body_limit_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(writes, "MAX_UPLOAD_BYTES", 64)
    with pytest.raises(sessions.PlatformError, match="zips to over 64 bytes"):
        writes.zip_report(_report(tmp_path))


def test_a_refused_import_says_what_the_api_said(fake: FakeAPI, tmp_path: Path) -> None:
    fake.refuse(403, "role 'analyst' may not perform 'import_report'; it needs 'data-engineer' or above")

    result = _olf("report", "import", str(_report(tmp_path)))

    assert result.exit_code != 0
    assert "it needs 'data-engineer' or above" in result.stderr


def test_export_writes_the_bundle_into_the_report_directory_replacing_its_managed_assets(
    fake: FakeAPI, answers: None, tmp_path: Path
) -> None:
    target = _report(tmp_path / "orders")

    result = _olf("report", "export", "orders v2", str(target))

    assert result.exit_code == 0, result.output
    assert result.stdout == f"Exported orders v2 from dev into {target}.\n"
    assert fake.requests[0]["path"] == "/api/v1/stages/dev/reports/orders%20v2/export"
    assert (target / DASHBOARD).read_text() == "dashboard_title: Orders\n"
    # The managed directories are replaced whole; what the report keeps beside them stays.
    assert not (target / DATASET).exists() and not (target / "databases").exists()
    assert (target / "README.md").read_text() == "notes"
