from pathlib import Path

import pytest
from _report_bundle_support import (
    CHART_UUID,
    DASHBOARD_UUID,
    DATABASE_UUID,
    DATASET_UUID,
    REPORT_DIR,
    write_report_bundle,
)

from olf.common import report_bundles


def test_validate_report_registry_rejects_declared_mounted_mismatch(tmp_path: Path) -> None:
    mounted = tmp_path / "lakehouse_code/dashboards/superset/mounted"
    mounted.mkdir(parents=True)
    (mounted / "metadata.yaml").write_text("type: assets\n")

    with pytest.raises(RuntimeError, match="declared but not mounted.*mounted but not declared"):
        report_bundles.validate_report_registry(
            tmp_path,
            ["lakehouse_code/dashboards/superset/declared"],
        )


def test_report_bundle_errors_accepts_a_promotable_bundle(tmp_path: Path) -> None:
    write_report_bundle(tmp_path)

    assert report_bundles.report_bundle_errors(tmp_path, REPORT_DIR) == []


def test_report_bundle_errors_reports_a_missing_bundle(tmp_path: Path) -> None:
    assert report_bundles.report_bundle_errors(tmp_path, REPORT_DIR) == [f"{REPORT_DIR}/metadata.yaml: missing"]


def test_report_bundle_errors_rejects_an_asset_without_a_stable_uuid(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(f"slice_name: Orders\ndataset_uuid: {DATASET_UUID}\n")

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("has no stable uuid" in error for error in errors)


def test_report_bundle_errors_rejects_a_duplicated_uuid(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "charts" / "second.yaml").write_text(
        f"slice_name: Copy\nuuid: {CHART_UUID}\ndataset_uuid: {DATASET_UUID}\n"
    )

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("is already used by" in error for error in errors)


def test_report_bundle_errors_rejects_a_dangling_dataset_reference(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: {CHART_UUID}\ndataset_uuid: {DASHBOARD_UUID}\n"
    )

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("dataset_uuid" in error and "does not resolve inside the bundle" in error for error in errors)


def test_report_bundle_errors_rejects_a_dashboard_referencing_an_absent_chart(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").unlink()

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("the bundle does not define" in error for error in errors)


def test_report_bundle_errors_rejects_a_personal_workspace_dependency(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\nschema: ws_alice_order_revenue_gold\nuuid: {DATASET_UUID}\n"
        f"database_uuid: {DATABASE_UUID}\n"
    )

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("personal workspace identifier" in error for error in errors)


def test_report_bundle_errors_rejects_a_stage_bound_physical_name(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/lakehouse_prod\nuuid: {DATABASE_UUID}\n"
    )

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("stage-bound physical name" in error for error in errors)


def test_validate_report_bundles_spans_every_declared_bundle(tmp_path: Path) -> None:
    write_report_bundle(tmp_path)
    other = "lakehouse_code/dashboards/superset/other"

    assert report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, other]) == [f"{other}/metadata.yaml: missing"]


_SECOND_REPORT_DIR = "lakehouse_code/dashboards/superset/second"


def test_two_bundles_may_share_one_database_identity(tmp_path: Path) -> None:
    """Every dashboard reads the same Gold through one Trino connection, so
    the shared database uuid is the intended arrangement, not a collision."""
    write_report_bundle(tmp_path)
    write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid="53333333-3333-5333-8333-333333333333",
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )

    assert report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, _SECOND_REPORT_DIR]) == []


@pytest.mark.parametrize("shared", ["dataset_uuid", "chart_uuid", "dashboard_uuid"])
def test_two_bundles_may_not_share_a_chart_dataset_or_dashboard_identity(tmp_path: Path, shared: str) -> None:
    """`deploy_reports` imports every declared bundle into one Superset, so a
    reused identity makes the second import overwrite the first asset."""
    distinct = {
        "dataset_uuid": "52222222-2222-5222-8222-222222222222",
        "chart_uuid": "53333333-3333-5333-8333-333333333333",
        "dashboard_uuid": "54444444-4444-5444-8444-444444444444",
    }
    del distinct[shared]
    write_report_bundle(tmp_path)
    write_report_bundle(tmp_path, _SECOND_REPORT_DIR, **distinct)

    errors = report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, _SECOND_REPORT_DIR])

    assert any(REPORT_DIR in error for error in errors), errors
    assert all(error.startswith(_SECOND_REPORT_DIR) for error in errors), errors


def test_report_bundle_errors_rejects_a_stage_prefixed_physical_schema(tmp_path: Path) -> None:
    """AWS prefixes every schema with the stage's catalog name, which is what
    `build_report_bundle`'s `schema_prefix` produces. A trailing word boundary
    would miss it, because `_` is a word character."""
    bundle = write_report_bundle(tmp_path)
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\nschema: lakehouse_dev_order_revenue_gold\nuuid: {DATASET_UUID}\n"
        f"database_uuid: {DATABASE_UUID}\n"
    )

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("stage-bound physical name" in error for error in errors)


def test_report_bundle_errors_allows_a_word_that_merely_starts_with_a_stage_name(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Lakehouse Development Notes\nuuid: {CHART_UUID}\ndataset_uuid: {DATASET_UUID}\n"
    )

    assert report_bundles.report_bundle_errors(tmp_path, REPORT_DIR) == []


def test_report_bundle_errors_rejects_an_identity_that_is_not_a_uuid(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: not-a-uuid\ndataset_uuid: {DATASET_UUID}\n"
    )

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert any("is not a UUID" in error for error in errors)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ("version: 1.0.0\ntype: Dashboard\n", "not 'assets'"),
        ("version: 1.0.0\n", "not 'assets'"),
        ("type: assets\n", "no export format version"),
        ("- not a mapping\n", "not a YAML mapping"),
    ],
)
def test_report_bundle_errors_rejects_metadata_the_importer_would_refuse(
    tmp_path: Path, metadata: str, expected: str
) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "metadata.yaml").write_text(metadata)

    assert [expected in error for error in report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)] == [True]


def test_two_bundles_may_not_define_one_shared_database_differently(tmp_path: Path) -> None:
    """A shared uuid means one connection; letting the definitions diverge
    lets the later import replace what every earlier dashboard reads."""
    write_report_bundle(tmp_path)
    second = write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid="53333333-3333-5333-8333-333333333333",
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )
    (second / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/iceberg\n"
        f"allow_dml: true\nuuid: {DATABASE_UUID}\n"
    )

    errors = report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, _SECOND_REPORT_DIR])

    assert any("defines a different database" in error for error in errors), errors


def test_a_shared_database_may_still_carry_a_stage_resolved_uri(tmp_path: Path) -> None:
    """`build_report_bundle` rewrites `sqlalchemy_uri` while packaging, so its
    checked-in value is a placeholder and cannot be part of the comparison."""
    write_report_bundle(tmp_path)
    second = write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid="53333333-3333-5333-8333-333333333333",
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )
    (second / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://other@trino:8080/iceberg\nuuid: {DATABASE_UUID}\n"
    )

    assert report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, _SECOND_REPORT_DIR]) == []


_HEX_UUID = "3a3b3c3d-3333-5333-8333-3333333333ff"


def test_two_bundles_may_not_claim_one_identity_in_different_cases(tmp_path: Path) -> None:
    """Superset resolves both spellings to the same asset, so comparing raw
    strings would let the second import overwrite the first."""
    write_report_bundle(tmp_path, chart_uuid=_HEX_UUID)
    write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid=_HEX_UUID.upper(),
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )

    errors = report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, _SECOND_REPORT_DIR])

    assert any("is already used by" in error for error in errors), errors


def test_a_reference_resolves_whatever_case_it_is_spelled_in(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path, dataset_uuid=_HEX_UUID)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: {CHART_UUID}\ndataset_uuid: {_HEX_UUID.upper()}\n"
    )

    assert report_bundles.report_bundle_errors(tmp_path, REPORT_DIR) == []


def test_malformed_yaml_is_reported_against_its_own_path(tmp_path: Path) -> None:
    """An editing or merge mistake must name the file and leave the rest of
    the run intact, not abort the validator with a traceback."""
    bundle = write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text("slice_name: [unterminated\n")
    other = "lakehouse_code/dashboards/superset/other"

    errors = report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, other])

    assert any(error.startswith(f"{REPORT_DIR}/charts/chart.yaml: is not valid YAML") for error in errors), errors
    assert f"{other}/metadata.yaml: missing" in errors


def test_malformed_bundle_metadata_is_reported_against_its_own_path(tmp_path: Path) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "metadata.yaml").write_text("type: [unterminated\n")

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert [error.startswith(f"{REPORT_DIR}/metadata.yaml: is not valid YAML") for error in errors] == [True]


def test_a_bundle_exporting_no_dashboard_is_not_promotable(tmp_path: Path) -> None:
    """What `olf product new --with-report` leaves behind: a database and its
    datasets, awaiting a dashboard authored in Superset. `e2e._assertions`
    already refuses it at runtime, so freezing it would publish an incomplete
    revision and surface the omission only after promotion."""
    bundle = write_report_bundle(tmp_path)
    (bundle / "dashboards" / "dash.yaml").unlink()
    (bundle / "charts" / "chart.yaml").unlink()

    errors = report_bundles.report_bundle_errors(tmp_path, REPORT_DIR)

    assert [f"{REPORT_DIR}: exports no Superset dashboard" in error for error in errors] == [True]


@pytest.mark.parametrize(
    "schema_line",
    [
        pytest.param('schema: "order_revenue_gold"', id="double-quoted"),
        pytest.param("schema: 'order_revenue_gold'", id="single-quoted"),
        pytest.param("schema: order_revenue_gold  # the Gold mart", id="trailing-comment"),
        pytest.param("schema: >-\n  order_revenue_gold", id="block-scalar"),
    ],
)
def test_report_bundle_errors_accepts_valid_yaml_schema_styles(tmp_path: Path, schema_line: str) -> None:
    bundle = write_report_bundle(tmp_path)
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\n{schema_line}\nuuid: {DATASET_UUID}\ndatabase_uuid: {DATABASE_UUID}\n"
    )

    assert report_bundles.report_bundle_errors(tmp_path, REPORT_DIR) == []


def test_two_bundles_may_share_a_database_spelled_in_different_cases(tmp_path: Path) -> None:
    """`_canonical_uuid` treats the two spellings as one asset, so comparing
    the raw `uuid` would report identical databases as conflicting."""
    write_report_bundle(tmp_path, database_uuid=_HEX_UUID)
    second = write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        database_uuid=_HEX_UUID,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid="53333333-3333-5333-8333-333333333333",
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )
    (second / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/iceberg\nuuid: {_HEX_UUID.upper()}\n"
    )

    assert report_bundles.validate_report_bundles(tmp_path, [REPORT_DIR, _SECOND_REPORT_DIR]) == []
