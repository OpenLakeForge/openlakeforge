import contextlib
import io
import json
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml
from conftest import FakeSuperset

from olf import k8s, superset
from olf.contracts import build_contract_env
from olf.profile import resolve_topology, validate_deployment_profile

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _stage_environment(fixture_name: str, stage: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """One stage's hydrated contract environment, as the CLI would see it.

    Mirrors `deployment.contract_env.applied_contract_environment`: the
    exports overlay the caller's own environment rather than replacing it,
    which is what lets a caller-set OPENLAKEFORGE_QUERY_SQLALCHEMY_URI
    survive (`olf.contracts` module docstring) and therefore what makes the
    stale-URI case below reachable at all.
    """
    contract = json.loads((FIXTURES / fixture_name).read_text())
    deployment = contract["deployment"]
    topology = resolve_topology(
        validate_deployment_profile(
            {
                "apiVersion": "openlakeforge.io/v1alpha1",
                "kind": "DeploymentProfile",
                "metadata": {"name": deployment["profile_name"]},
                "spec": {
                    "provider": {
                        "type": deployment["provider"],
                        **({"region": deployment["region"]} if deployment["region"] else {}),
                    },
                    "preset": "slim",
                    "stages": {
                        name: {
                            "enabled": True,
                            "capabilities": {
                                "analytics": "reporting" in payload,
                                "governance": "governance" in payload,
                            },
                        }
                        for name, payload in contract["stages"].items()
                    },
                },
            }
        )
    )
    resolved = dict(base or {})
    exports, unsets = build_contract_env(resolved, contract, repo_root=REPO_ROOT, topology=topology, stage=stage)
    resolved.update(exports)
    for name in unsets:
        resolved.pop(name, None)
    return resolved


def test_resolve_stage_report_target_reads_the_named_stages_own_bindings() -> None:
    dev = superset.resolve_stage_report_target(
        _stage_environment("aws-provider-contracts-v3.json", "dev"), stage="dev"
    )
    prod = superset.resolve_stage_report_target(
        _stage_environment("aws-provider-contracts-v3.json", "prod"), stage="prod"
    )

    assert (dev.namespace, prod.namespace) == ("olf-dev", "olf-prod")
    assert dev.sqlalchemy_uri.endswith("/lakehouse_dev")
    assert prod.sqlalchemy_uri.endswith("/lakehouse_prod")
    assert (dev.schema_prefix, prod.schema_prefix) == ("lakehouse_dev_", "lakehouse_prod_")


def test_resolve_stage_report_target_names_the_stage_its_catalog_serves() -> None:
    target = superset.resolve_stage_report_target(_stage_environment("aws-provider-contracts-v3.json", "uat"))

    assert target.stage == "uat"


def test_resolve_stage_report_target_fails_closed_without_analytics() -> None:
    environ = _stage_environment("local-provider-contracts-v3.json", "dev")

    with pytest.raises(superset.ReportStageError, match="analytics disabled"):
        superset.resolve_stage_report_target(environ, stage="dev")


def test_resolve_stage_report_target_rejects_an_unhydrated_environment() -> None:
    with pytest.raises(superset.ReportStageError, match="OPENLAKEFORGE_KUBE_NAMESPACE"):
        superset.resolve_stage_report_target({}, stage="dev")


def test_bundle_identity_from_source_dir() -> None:
    identity = superset.bundle_identity("lakehouse_code/dashboards/superset/order_revenue")
    assert identity.root == "order_revenue_superset_bundle"
    assert identity.name == "order_revenue_superset_bundle.zip"


def test_validate_report_registry_rejects_declared_mounted_mismatch(tmp_path: Path) -> None:
    mounted = tmp_path / "lakehouse_code/dashboards/superset/mounted"
    mounted.mkdir(parents=True)
    (mounted / "metadata.yaml").write_text("type: assets\n")

    with pytest.raises(RuntimeError, match="declared but not mounted.*mounted but not declared"):
        superset.validate_report_registry(
            tmp_path,
            ["lakehouse_code/dashboards/superset/declared"],
        )


def _fail_if_called(*_args: object, **_kwargs: object) -> None:
    pytest.fail("must not reach a live Superset for an empty or invalid registry")


def test_deploy_reports_with_no_declared_dashboards_imports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#228: `olf product new --with-report` leaves its draft bundle
    undeclared, so a Full profile's first artifacts deploy runs with zero
    declared dashboards. That is a legitimate 'nothing to import' state, not
    a misconfiguration -- deploying must succeed, and without ever waiting
    for the Superset that has nothing to receive."""
    monkeypatch.setattr(k8s, "wait_for_rollout", _fail_if_called)
    monkeypatch.setattr(superset, "superset_base_url", _fail_if_called)

    superset.deploy_reports(
        tmp_path,
        "openlakeforge-dev",
        "trino://superset@trino:8080/iceberg",
        report_source_dir=None,
        declared_report_dirs=(),
        work_dir=tmp_path / "work",
        admin_password="admin",
        admin_username="admin",
    )


def test_deploy_reports_still_fails_for_a_declared_but_missing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-registry skip must not swallow a real misconfiguration: a
    dashboard declared in lakehouse.yaml whose bundle is missing from disk
    is not the same state as nothing being declared at all, and must still
    fail loudly (and, since it's a descriptor problem, before ever touching
    the cluster)."""
    monkeypatch.setattr(k8s, "wait_for_rollout", _fail_if_called)
    monkeypatch.setattr(superset, "superset_base_url", _fail_if_called)

    with pytest.raises(RuntimeError, match="declared but not mounted"):
        superset.deploy_reports(
            tmp_path,
            "openlakeforge-dev",
            "trino://superset@trino:8080/iceberg",
            report_source_dir=None,
            declared_report_dirs=("lakehouse_code/dashboards/superset/missing",),
            work_dir=tmp_path / "work",
            admin_password="admin",
            admin_username="admin",
        )


def _serve(monkeypatch: pytest.MonkeyPatch, server: FakeSuperset) -> None:
    @contextlib.contextmanager
    def base_url(_namespace: str) -> Iterator[str]:
        yield server.url

    monkeypatch.setattr(superset, "superset_base_url", base_url)


def test_deploy_reports_imports_each_declared_bundle_through_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_superset: FakeSuperset
) -> None:
    _write_report_bundle(tmp_path)
    _serve(monkeypatch, fake_superset)

    superset.deploy_reports(
        tmp_path,
        "openlakeforge-dev",
        "trino://olf-dev@trino:8080/lakehouse_dev",
        report_source_dir=None,
        declared_report_dirs=(_REPORT_DIR,),
        work_dir=tmp_path / "work",
        admin_username="admin",
        admin_password="secret",
    )

    (imported,) = fake_superset.imported
    with ZipFile(io.BytesIO(imported)) as bundle:
        root = superset.bundle_identity(_REPORT_DIR).root
        assert yaml.safe_load(bundle.read(f"{root}/metadata.yaml"))["type"] == "assets"
        database = yaml.safe_load(bundle.read(f"{root}/databases/trino.yaml"))
    assert database["sqlalchemy_uri"] == "trino://olf-dev@trino:8080/lakehouse_dev"


def test_export_report_writes_the_dashboard_back_as_an_assets_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_superset: FakeSuperset
) -> None:
    """The dashboard was renamed in DEV; the export still finds it by uuid
    and replaces the checked-in assets with what Superset returns."""
    target = _write_report_bundle(tmp_path)
    export = io.BytesIO()
    with ZipFile(export, "w") as bundle:
        bundle.writestr("dashboard_export_20261004T000000/metadata.yaml", "version: 1.0.0\ntype: Dashboard\n")
        bundle.writestr(
            "dashboard_export_20261004T000000/dashboards/Renamed_9.yaml",
            f"dashboard_title: Renamed\nuuid: {_DASHBOARD_UUID}\n",
        )
    fake_superset.dashboards[_DASHBOARD_UUID] = 9
    fake_superset.exports[9] = export.getvalue()
    _serve(monkeypatch, fake_superset)

    superset.export_report(
        tmp_path,
        "openlakeforge-dev",
        report_source_dir=_REPORT_DIR,
        bundle_name="demo.zip",
        work_dir=tmp_path / "work",
        admin_username="admin",
        admin_password="secret",
        dashboard=_DASHBOARD_UUID,
    )

    assert yaml.safe_load((target / "metadata.yaml").read_text()) == {"version": "1.0.0", "type": "assets"}
    assert [path.name for path in (target / "dashboards").iterdir()] == ["Renamed_9.yaml"]
    assert not (target / "charts").exists()


@pytest.mark.parametrize(
    "source_uri",
    [
        "sqlalchemy_uri: trino://old@host:8080/iceberg",
        'sqlalchemy_uri: "trino://old@host:8080/iceberg"',
        "sqlalchemy_uri: 'trino://old@host:8080/iceberg'",
        "sqlalchemy_uri: >-\n  trino://old@host:8080/iceberg",
        "sqlalchemy_uri: |-\n  trino://old@host:8080/iceberg",
        "sqlalchemy_uri: trino://old@host:8080/iceberg  # old connection",
        "sqlalchemy_uri:",
        "",
    ],
    ids=("plain", "double-quoted", "single-quoted", "folded", "literal", "trailing-comment", "empty", "missing"),
)
def test_build_report_bundle_rewrites_database_uri(tmp_path: Path, source_uri: str) -> None:
    source = tmp_path / "report"
    (source / "databases").mkdir(parents=True)
    (source / "dashboards").mkdir()
    (source / "databases" / "trino.yaml").write_text(
        f"database_name: trino\n{source_uri}\nuuid: database-id\n"
    )
    (source / "dashboards" / "d.yaml").write_text("dashboard_title: X\n")
    (source / "README.md").write_text("ignored")

    bundle_path = tmp_path / "bundle.zip"
    superset.build_report_bundle(source, bundle_path, "my_bundle", "trino://superset@trino:8080/iceberg")

    with ZipFile(bundle_path) as bundle:
        names = set(bundle.namelist())
        assert names == {"my_bundle/databases/trino.yaml", "my_bundle/dashboards/d.yaml"}
        database = yaml.safe_load(bundle.read("my_bundle/databases/trino.yaml"))
        assert database["sqlalchemy_uri"] == "trino://superset@trino:8080/iceberg"
        assert database["uuid"] == "database-id"


@pytest.mark.parametrize(
    "schema",
    [
        "schema: order_revenue_gold",
        'schema: "order_revenue_gold"',
        "schema: 'order_revenue_gold'",
        "schema: >-\n  order_revenue_gold",
        "schema: order_revenue_gold  # the Gold mart",
    ],
    ids=("plain", "double-quoted", "single-quoted", "folded", "trailing-comment"),
)
def test_build_report_bundle_prefixes_dataset_schemas(tmp_path: Path, schema: str) -> None:
    source = tmp_path / "report"
    (source / "databases").mkdir(parents=True)
    (source / "datasets" / "OpenLakeForge_Trino").mkdir(parents=True)
    (source / "dashboards").mkdir()
    (source / "databases" / "trino.yaml").write_text(
        "database_name: trino\nsqlalchemy_uri: trino://old@host:8080/iceberg\n"
    )
    (source / "datasets" / "OpenLakeForge_Trino" / "mart_x.yaml").write_text(
        f"table_name: mart_x\n{schema}\nuuid: abc\n"
    )
    (source / "dashboards" / "d.yaml").write_text("dashboard_title: X\n")

    bundle_path = tmp_path / "bundle.zip"
    superset.build_report_bundle(
        source, bundle_path, "my_bundle", "trino://superset@trino:8080/iceberg", schema_prefix="lakehouse_dev_"
    )

    with ZipFile(bundle_path) as bundle:
        dataset = yaml.safe_load(bundle.read("my_bundle/datasets/OpenLakeForge_Trino/mart_x.yaml"))
        assert dataset["schema"] == "lakehouse_dev_order_revenue_gold"


@pytest.mark.parametrize("schema", ["schema:", ""], ids=("empty", "missing"))
def test_build_report_bundle_rejects_a_dataset_without_a_schema(tmp_path: Path, schema: str) -> None:
    source = tmp_path / "report"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "mart_x.yaml").write_text(f"table_name: mart_x\n{schema}\nuuid: abc\n")

    with pytest.raises(ValueError, match="has no schema to prefix"):
        superset.build_report_bundle(
            source,
            tmp_path / "bundle.zip",
            "my_bundle",
            "trino://superset@trino:8080/iceberg",
            schema_prefix="lakehouse_dev_",
        )


def test_build_report_bundle_leaves_dataset_schemas_alone_without_a_prefix(tmp_path: Path) -> None:
    source = tmp_path / "report"
    (source / "datasets" / "OpenLakeForge_Trino").mkdir(parents=True)
    (source / "datasets" / "OpenLakeForge_Trino" / "mart_x.yaml").write_text(
        "table_name: mart_x\nschema: order_revenue_gold\nuuid: abc\n"
    )

    bundle_path = tmp_path / "bundle.zip"
    superset.build_report_bundle(source, bundle_path, "my_bundle", "trino://superset@trino:8080/iceberg")

    with ZipFile(bundle_path) as bundle:
        dataset = bundle.read("my_bundle/datasets/OpenLakeForge_Trino/mart_x.yaml").decode()
        assert "schema: order_revenue_gold" in dataset


def test_unpack_export_bundle_replaces_managed_assets(tmp_path: Path) -> None:
    bundle_path = tmp_path / "export.zip"
    with ZipFile(bundle_path, "w") as bundle:
        bundle.writestr("root/metadata.yaml", "type: assets\n")
        bundle.writestr("root/dashboards/d.yaml", "dashboard_title: X\n")
        bundle.writestr("root/.hidden.yaml", "ignored")

    target = tmp_path / "out"
    (target / "dashboards").mkdir(parents=True)
    (target / "dashboards" / "stale.yaml").write_text("stale")

    superset.unpack_export_bundle(bundle_path, target)

    assert (target / "metadata.yaml").read_text() == "type: assets\n"
    assert (target / "dashboards" / "d.yaml").exists()
    assert not (target / "dashboards" / "stale.yaml").exists()


def test_a_stage_without_an_applied_contract_is_refused() -> None:
    """Values alone prove nothing: `build_contract_env` synthesizes a
    dev-shaped environment and keeps caller-exported values when the
    Terraform output is unavailable. A shell still holding deployment A's
    canonical `lakehouse_prod` bindings would otherwise import into
    deployment B's Superset with A's Trino URI."""
    stale = {
        "OPENLAKEFORGE_ANALYTICS_ENABLED": "true",
        "OPENLAKEFORGE_KUBE_NAMESPACE": "olf-prod",
        "OPENLAKEFORGE_DBT_TRINO_USER": "olf-prod-runtime",
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.deployment-a",
        "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
        "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_prod",
        "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI": "trino://olf-prod@trino.deployment-a:8080/lakehouse_prod",
    }

    with pytest.raises(superset.ReportStageError, match="no applied provider contract"):
        superset.resolve_stage_report_target(stale, stage="prod")


def test_a_contract_applied_for_another_stage_is_refused() -> None:
    environ = {
        "OPENLAKEFORGE_ANALYTICS_ENABLED": "true",
        "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
        "OPENLAKEFORGE_KUBE_NAMESPACE": "olf-dev",
        "OPENLAKEFORGE_DBT_TRINO_USER": "olf-dev-runtime",
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.olf-system",
        "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
        "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_dev",
        "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI": "trino://olf-dev@trino.olf-system:8080/lakehouse_dev",
    }

    with pytest.raises(superset.ReportStageError, match="serves 'dev'"):
        superset.resolve_stage_report_target(environ, stage="prod")


def test_the_v2_dev_compatibility_contract_still_resolves() -> None:
    """Pre-v3 platform state is DEV-only and names its catalog `iceberg`.
    The v2 adapter exists so report commands keep working against it until
    the next platform apply; the resolver must not demand a v3 catalog name."""
    from olf.contracts import build_contract_env

    contract = json.loads((FIXTURES / "local-provider-contracts.json").read_text())
    exports, _ = build_contract_env({}, contract, repo_root=Path(__file__).resolve().parents[3])

    target = superset.resolve_stage_report_target(exports, stage="dev")

    assert target.stage == "dev"
    assert target.sqlalchemy_uri.endswith("/iceberg")


@pytest.mark.parametrize(
    "stale",
    [
        pytest.param(None, id="another-stage"),
        pytest.param("trino://olf-prod-runtime@trino.deployment-a:8080/lakehouse_prod", id="another-deployment"),
        pytest.param("trino://olf-dev-runtime@trino:8080/lakehouse_prod", id="a-sibling-stage-user"),
    ],
)
def test_a_caller_exported_query_uri_never_reaches_the_target(stale: str | None) -> None:
    """`build_contract_env` keeps a caller-exported
    OPENLAKEFORGE_QUERY_SQLALCHEMY_URI even with a contract applied, and any
    of its parts can belong elsewhere: another stage's catalog, another
    deployment's endpoint, or a sibling stage's Trino user -- whose catalog
    rules would decide what SQL Lab on the imported connection can read.
    None of it is used; the URI comes from the applied contract."""
    if stale is None:
        stale = _stage_environment("aws-provider-contracts-v3.json", "dev")["OPENLAKEFORGE_QUERY_SQLALCHEMY_URI"]
    contract = _stage_environment("aws-provider-contracts-v3.json", "prod")
    environ = _stage_environment(
        "aws-provider-contracts-v3.json", "prod", base={"OPENLAKEFORGE_QUERY_SQLALCHEMY_URI": stale}
    )

    target = superset.resolve_stage_report_target(environ, stage="prod")

    assert environ["OPENLAKEFORGE_QUERY_SQLALCHEMY_URI"] == stale
    assert target.sqlalchemy_uri == contract["OPENLAKEFORGE_QUERY_SQLALCHEMY_URI"]


_REPORT_DIR = "lakehouse_code/dashboards/superset/demo"
_DATABASE_UUID = "11111111-1111-5111-8111-111111111111"
_DATASET_UUID = "22222222-2222-5222-8222-222222222222"
_CHART_UUID = "33333333-3333-5333-8333-333333333333"
_DASHBOARD_UUID = "44444444-4444-5444-8444-444444444444"


def _write_report_bundle(
    root: Path,
    report_dir: str = _REPORT_DIR,
    *,
    database_uuid: str = _DATABASE_UUID,
    dataset_uuid: str = _DATASET_UUID,
    chart_uuid: str = _CHART_UUID,
    dashboard_uuid: str = _DASHBOARD_UUID,
) -> Path:
    """A minimal promotable bundle shaped like a real Superset asset export."""
    bundle = root / report_dir
    for kind in ("databases", "datasets", "charts", "dashboards"):
        (bundle / kind).mkdir(parents=True)
    (bundle / "metadata.yaml").write_text("version: 1.0.0\ntype: assets\n")
    (bundle / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/iceberg\nuuid: {database_uuid}\n"
    )
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\nschema: order_revenue_gold\nuuid: {dataset_uuid}\n"
        f"database_uuid: {database_uuid}\n"
    )
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: {chart_uuid}\ndataset_uuid: {dataset_uuid}\n"
    )
    (bundle / "dashboards" / "dash.yaml").write_text(
        f"dashboard_title: Orders\nslug: orders\nuuid: {dashboard_uuid}\n"
        "position:\n"
        "  CHART-ORDERS:\n"
        "    id: CHART-ORDERS\n"
        "    type: CHART\n"
        "    meta:\n"
        f"      uuid: {chart_uuid}\n"
    )
    return bundle


def test_report_bundle_errors_accepts_a_promotable_bundle(tmp_path: Path) -> None:
    _write_report_bundle(tmp_path)

    assert superset.report_bundle_errors(tmp_path, _REPORT_DIR) == []


def test_report_bundle_errors_reports_a_missing_bundle(tmp_path: Path) -> None:
    assert superset.report_bundle_errors(tmp_path, _REPORT_DIR) == [f"{_REPORT_DIR}/metadata.yaml: missing"]


def test_report_bundle_errors_rejects_an_asset_without_a_stable_uuid(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(f"slice_name: Orders\ndataset_uuid: {_DATASET_UUID}\n")

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert any("has no stable uuid" in error for error in errors)


def test_report_bundle_errors_rejects_a_duplicated_uuid(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "charts" / "second.yaml").write_text(
        f"slice_name: Copy\nuuid: {_CHART_UUID}\ndataset_uuid: {_DATASET_UUID}\n"
    )

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert any("is already used by" in error for error in errors)


def test_report_bundle_errors_rejects_a_dangling_dataset_reference(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: {_CHART_UUID}\ndataset_uuid: {_DASHBOARD_UUID}\n"
    )

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert any("dataset_uuid" in error and "does not resolve inside the bundle" in error for error in errors)


def test_report_bundle_errors_rejects_a_dashboard_referencing_an_absent_chart(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").unlink()

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert any("the bundle does not define" in error for error in errors)


def test_report_bundle_errors_rejects_a_personal_workspace_dependency(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\nschema: ws_alice_order_revenue_gold\nuuid: {_DATASET_UUID}\n"
        f"database_uuid: {_DATABASE_UUID}\n"
    )

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert any("personal workspace identifier" in error for error in errors)


def test_report_bundle_errors_rejects_a_stage_bound_physical_name(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/lakehouse_prod\nuuid: {_DATABASE_UUID}\n"
    )

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert any("stage-bound physical name" in error for error in errors)


def test_validate_report_bundles_spans_every_declared_bundle(tmp_path: Path) -> None:
    _write_report_bundle(tmp_path)
    other = "lakehouse_code/dashboards/superset/other"

    assert superset.validate_report_bundles(tmp_path, [_REPORT_DIR, other]) == [f"{other}/metadata.yaml: missing"]


_SECOND_REPORT_DIR = "lakehouse_code/dashboards/superset/second"


def test_two_bundles_may_share_one_database_identity(tmp_path: Path) -> None:
    """Every dashboard reads the same Gold through one Trino connection, so
    the shared database uuid is the intended arrangement, not a collision."""
    _write_report_bundle(tmp_path)
    _write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid="53333333-3333-5333-8333-333333333333",
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )

    assert superset.validate_report_bundles(tmp_path, [_REPORT_DIR, _SECOND_REPORT_DIR]) == []


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
    _write_report_bundle(tmp_path)
    _write_report_bundle(tmp_path, _SECOND_REPORT_DIR, **distinct)

    errors = superset.validate_report_bundles(tmp_path, [_REPORT_DIR, _SECOND_REPORT_DIR])

    assert any(_REPORT_DIR in error for error in errors), errors
    assert all(error.startswith(_SECOND_REPORT_DIR) for error in errors), errors


def test_report_bundle_errors_rejects_a_stage_prefixed_physical_schema(tmp_path: Path) -> None:
    """AWS prefixes every schema with the stage's catalog name, which is what
    `build_report_bundle`'s `schema_prefix` produces. A trailing word boundary
    would miss it, because `_` is a word character."""
    bundle = _write_report_bundle(tmp_path)
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\nschema: lakehouse_dev_order_revenue_gold\nuuid: {_DATASET_UUID}\n"
        f"database_uuid: {_DATABASE_UUID}\n"
    )

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert any("stage-bound physical name" in error for error in errors)


def test_report_bundle_errors_allows_a_word_that_merely_starts_with_a_stage_name(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Lakehouse Development Notes\nuuid: {_CHART_UUID}\ndataset_uuid: {_DATASET_UUID}\n"
    )

    assert superset.report_bundle_errors(tmp_path, _REPORT_DIR) == []


def test_report_bundle_errors_rejects_an_identity_that_is_not_a_uuid(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: not-a-uuid\ndataset_uuid: {_DATASET_UUID}\n"
    )

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

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
    bundle = _write_report_bundle(tmp_path)
    (bundle / "metadata.yaml").write_text(metadata)

    assert [expected in error for error in superset.report_bundle_errors(tmp_path, _REPORT_DIR)] == [True]


def test_two_bundles_may_not_define_one_shared_database_differently(tmp_path: Path) -> None:
    """A shared uuid means one connection; letting the definitions diverge
    lets the later import replace what every earlier dashboard reads."""
    _write_report_bundle(tmp_path)
    second = _write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid="53333333-3333-5333-8333-333333333333",
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )
    (second / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/iceberg\n"
        f"allow_dml: true\nuuid: {_DATABASE_UUID}\n"
    )

    errors = superset.validate_report_bundles(tmp_path, [_REPORT_DIR, _SECOND_REPORT_DIR])

    assert any("defines a different database" in error for error in errors), errors


def test_a_shared_database_may_still_carry_a_stage_resolved_uri(tmp_path: Path) -> None:
    """`build_report_bundle` rewrites `sqlalchemy_uri` while packaging, so its
    checked-in value is a placeholder and cannot be part of the comparison."""
    _write_report_bundle(tmp_path)
    second = _write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid="53333333-3333-5333-8333-333333333333",
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )
    (second / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://other@trino:8080/iceberg\nuuid: {_DATABASE_UUID}\n"
    )

    assert superset.validate_report_bundles(tmp_path, [_REPORT_DIR, _SECOND_REPORT_DIR]) == []


_HEX_UUID = "3a3b3c3d-3333-5333-8333-3333333333ff"


def test_two_bundles_may_not_claim_one_identity_in_different_cases(tmp_path: Path) -> None:
    """Superset resolves both spellings to the same asset, so comparing raw
    strings would let the second import overwrite the first."""
    _write_report_bundle(tmp_path, chart_uuid=_HEX_UUID)
    _write_report_bundle(
        tmp_path,
        _SECOND_REPORT_DIR,
        dataset_uuid="52222222-2222-5222-8222-222222222222",
        chart_uuid=_HEX_UUID.upper(),
        dashboard_uuid="54444444-4444-5444-8444-444444444444",
    )

    errors = superset.validate_report_bundles(tmp_path, [_REPORT_DIR, _SECOND_REPORT_DIR])

    assert any("is already used by" in error for error in errors), errors


def test_a_reference_resolves_whatever_case_it_is_spelled_in(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path, dataset_uuid=_HEX_UUID)
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: {_CHART_UUID}\ndataset_uuid: {_HEX_UUID.upper()}\n"
    )

    assert superset.report_bundle_errors(tmp_path, _REPORT_DIR) == []


def test_malformed_yaml_is_reported_against_its_own_path(tmp_path: Path) -> None:
    """An editing or merge mistake must name the file and leave the rest of
    the run intact, not abort the validator with a traceback."""
    bundle = _write_report_bundle(tmp_path)
    (bundle / "charts" / "chart.yaml").write_text("slice_name: [unterminated\n")
    other = "lakehouse_code/dashboards/superset/other"

    errors = superset.validate_report_bundles(tmp_path, [_REPORT_DIR, other])

    assert any(error.startswith(f"{_REPORT_DIR}/charts/chart.yaml: is not valid YAML") for error in errors), errors
    assert f"{other}/metadata.yaml: missing" in errors


def test_malformed_bundle_metadata_is_reported_against_its_own_path(tmp_path: Path) -> None:
    bundle = _write_report_bundle(tmp_path)
    (bundle / "metadata.yaml").write_text("type: [unterminated\n")

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert [error.startswith(f"{_REPORT_DIR}/metadata.yaml: is not valid YAML") for error in errors] == [True]


def test_a_bundle_exporting_no_dashboard_is_not_promotable(tmp_path: Path) -> None:
    """What `olf product new --with-report` leaves behind: a database and its
    datasets, awaiting a dashboard authored in Superset. `e2e._assertions`
    already refuses it at runtime, so freezing it would publish an incomplete
    revision and surface the omission only after promotion."""
    bundle = _write_report_bundle(tmp_path)
    (bundle / "dashboards" / "dash.yaml").unlink()
    (bundle / "charts" / "chart.yaml").unlink()

    errors = superset.report_bundle_errors(tmp_path, _REPORT_DIR)

    assert [f"{_REPORT_DIR}: exports no Superset dashboard" in error for error in errors] == [True]


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
    bundle = _write_report_bundle(tmp_path)
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\n{schema_line}\nuuid: {_DATASET_UUID}\ndatabase_uuid: {_DATABASE_UUID}\n"
    )

    assert superset.report_bundle_errors(tmp_path, _REPORT_DIR) == []


def test_two_bundles_may_share_a_database_spelled_in_different_cases(tmp_path: Path) -> None:
    """`_canonical_uuid` treats the two spellings as one asset, so comparing
    the raw `uuid` would report identical databases as conflicting."""
    _write_report_bundle(tmp_path, database_uuid=_HEX_UUID)
    second = _write_report_bundle(
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

    assert superset.validate_report_bundles(tmp_path, [_REPORT_DIR, _SECOND_REPORT_DIR]) == []
