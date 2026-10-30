import contextlib
import io
import json
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml
from _report_bundle_support import DASHBOARD_UUID, DATABASE_UUID, REPORT_DIR, write_report_bundle
from conftest import FakeSuperset
from openlakeforge_services import reports

from olf.common import report_bundles
from olf.common.profile import resolve_topology, validate_deployment_profile
from olf.infra import k8s, superset
from olf.infra.contracts import build_contract_env

FIXTURES = Path(__file__).parents[1] / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[4]


_EXTERNAL_IDENTITY = {
    "issuer": "external",
    "issuer_url": "https://login.example.com/",
    "role_claim": "groups",
    "role_mapping": {"platform-admin": ["olf-admins"]},
}

def _stage_environment(fixture_name: str, stage: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """One stage's hydrated contract environment, as the CLI would see it.

    Mirrors `deployment.contract_env.applied_contract_environment`: the
    exports overlay the caller's own environment rather than replacing it,
    which is what lets a caller-set OPENLAKEFORGE_QUERY_SQLALCHEMY_URI
    survive (`olf.infra.contracts` module docstring) and therefore what makes the
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
                    **({"access": {}} if contract["shared"]["access"]["implementation"] == "access.ingress" else {}),
                    # Authentication off is local-only, so cloud fixtures carry an identity block.
                    **({"identity": _EXTERNAL_IDENTITY} if deployment["provider"] != "local" else {}),
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
    identity = reports.bundle_identity("lakehouse_code/dashboards/superset/order_revenue")
    assert identity.root == "order_revenue_superset_bundle"
    assert identity.name == "order_revenue_superset_bundle.zip"


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
    dashboard whose bundle is missing from disk is not the same state as no
    dashboards at all, and must still fail loudly (and, since it's a
    descriptor problem, before ever touching the cluster)."""
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
    write_report_bundle(tmp_path)
    _serve(monkeypatch, fake_superset)
    fake_superset.permissions[1] = ("schema_access", "[Trino].[order_revenue_gold]")

    superset.deploy_reports(
        tmp_path,
        "openlakeforge-dev",
        "trino://olf-dev@trino:8080/lakehouse_dev",
        report_source_dir=None,
        declared_report_dirs=(REPORT_DIR,),
        work_dir=tmp_path / "work",
        admin_username="admin",
        admin_password="secret",
    )

    (imported,) = fake_superset.imported
    with ZipFile(io.BytesIO(imported)) as bundle:
        root = reports.bundle_identity(REPORT_DIR).root
        assert yaml.safe_load(bundle.read(f"{root}/metadata.yaml"))["type"] == "assets"
        database = yaml.safe_load(bundle.read(f"{root}/databases/trino.yaml"))
    assert database["sqlalchemy_uri"] == "trino://olf-dev@trino:8080/lakehouse_dev"


def test_export_report_writes_the_dashboard_back_as_an_assets_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_superset: FakeSuperset
) -> None:
    """The dashboard was renamed in DEV; the export still finds it by uuid
    and replaces the checked-in assets with what Superset returns."""
    target = write_report_bundle(tmp_path)
    export = io.BytesIO()
    with ZipFile(export, "w") as bundle:
        bundle.writestr("dashboard_export_20261004T000000/metadata.yaml", "version: 1.0.0\ntype: Dashboard\n")
        bundle.writestr(
            "dashboard_export_20261004T000000/dashboards/Renamed_9.yaml",
            f"dashboard_title: Renamed\nuuid: {DASHBOARD_UUID}\n",
        )
    fake_superset.dashboards[DASHBOARD_UUID] = 9
    fake_superset.exports[9] = export.getvalue()
    _serve(monkeypatch, fake_superset)

    superset.export_report(
        tmp_path,
        "openlakeforge-dev",
        report_source_dir=REPORT_DIR,
        bundle_name="demo.zip",
        work_dir=tmp_path / "work",
        admin_username="admin",
        admin_password="secret",
        dashboard=DASHBOARD_UUID,
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
    reports.build_report_bundle(source, bundle_path, "my_bundle", "trino://superset@trino:8080/iceberg")

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
    reports.build_report_bundle(
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
        reports.build_report_bundle(
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
    reports.build_report_bundle(source, bundle_path, "my_bundle", "trino://superset@trino:8080/iceberg")

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

    reports.unpack_export_bundle(bundle_path, target)

    assert (target / "metadata.yaml").read_text() == "type: assets\n"
    assert (target / "dashboards" / "d.yaml").exists()
    assert not (target / "dashboards" / "stale.yaml").exists()


def _bundle_documents(bundle: Path) -> dict[str, object]:
    return {
        path.relative_to(bundle).as_posix(): yaml.safe_load(path.read_text())
        for path in sorted(bundle.rglob("*.yaml"))
    }


@pytest.mark.parametrize(
    ("stage_uri", "schema_prefix", "extra"),
    [
        ("trino://olf-dev-runtime@trino.olf-system:8080/lakehouse_dev", "", None),
        (
            "trino://olf-dev-runtime@trino.olf-system:8443/lakehouse_dev",
            "lakehouse_dev_",
            {"engine_params": {"connect_args": {"http_scheme": "https", "verify": reports.SUPERSET_QUERY_CA}}},
        ),
    ],
    ids=("local", "aws-with-tls"),
)
def test_an_export_of_an_imported_bundle_is_stage_neutral_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_superset: FakeSuperset,
    stage_uri: str,
    schema_prefix: str,
    extra: dict[str, object] | None,
) -> None:
    """Superset exports what was imported into it: the stage's own connection
    and physical schemas. Exporting must hand back the bundle that was checked
    in, which `olf report validate` accepts without hand edits."""
    checked_in = write_report_bundle(tmp_path)
    (checked_in / "databases" / "trino.yaml").write_text(
        "database_name: Trino\n"
        f"sqlalchemy_uri: {reports.STAGE_NEUTRAL_SQLALCHEMY_URI}\n"
        "extra:\n  metadata_params: {}\n  engine_params: {}\n"
        f"uuid: {DATABASE_UUID}\n"
    )
    expected = _bundle_documents(checked_in)
    imported = tmp_path / "imported.zip"
    reports.build_report_bundle(
        checked_in, imported, "dashboard_export_20261020T000000", stage_uri, schema_prefix=schema_prefix, extra=extra
    )
    with ZipFile(imported) as bundle:
        assert any("lakehouse_dev" in bundle.read(name).decode() for name in bundle.namelist())
    fake_superset.dashboards[DASHBOARD_UUID] = 9
    fake_superset.exports[9] = imported.read_bytes()
    _serve(monkeypatch, fake_superset)

    superset.export_report(
        tmp_path,
        "olf-dev",
        report_source_dir=REPORT_DIR,
        bundle_name="demo.zip",
        work_dir=tmp_path / "work",
        admin_username="admin",
        admin_password="secret",
        dashboard=DASHBOARD_UUID,
        schema_prefix=schema_prefix,
    )

    assert _bundle_documents(checked_in) == expected
    assert report_bundles.report_bundle_errors(tmp_path, REPORT_DIR) == []


def test_unpack_export_bundle_keeps_connect_args_it_did_not_add(tmp_path: Path) -> None:
    bundle_path = tmp_path / "export.zip"
    with ZipFile(bundle_path, "w") as bundle:
        bundle.writestr(
            "root/databases/trino.yaml",
            "sqlalchemy_uri: trino://u@h:8443/lakehouse_prod\n"
            "extra:\n  engine_params:\n    connect_args:\n      http_scheme: https\n      timezone: UTC\n",
        )
        bundle.writestr("root/datasets/mart.yaml", "table_name: mart\nschema: order_revenue_gold\n")

    reports.unpack_export_bundle(bundle_path, tmp_path / "out", schema_prefix="lakehouse_prod_")

    database = yaml.safe_load((tmp_path / "out" / "databases" / "trino.yaml").read_text())
    assert database["sqlalchemy_uri"] == reports.STAGE_NEUTRAL_SQLALCHEMY_URI
    assert database["extra"] == {"engine_params": {"connect_args": {"timezone": "UTC"}}}
    assert yaml.safe_load((tmp_path / "out" / "datasets" / "mart.yaml").read_text())["schema"] == "order_revenue_gold"


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
    from olf.infra.contracts import build_contract_env

    contract = json.loads((FIXTURES / "local-provider-contracts.json").read_text())
    exports, _ = build_contract_env({}, contract, repo_root=Path(__file__).resolve().parents[4])

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


def _applied_environment(**tls: str) -> dict[str, str]:
    return {
        "OPENLAKEFORGE_ANALYTICS_ENABLED": "true",
        "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
        "OPENLAKEFORGE_KUBE_NAMESPACE": "olf-dev",
        "OPENLAKEFORGE_DBT_TRINO_USER": "olf-dev-runtime",
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.olf-system",
        "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
        "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_dev",
        **tls,
    }


def test_a_tls_query_contract_targets_https_with_the_ca_and_an_off_one_is_unchanged(tmp_path: Path) -> None:
    plain = superset.resolve_stage_report_target(_applied_environment(), stage="dev")
    tls = superset.resolve_stage_report_target(
        _applied_environment(
            OPENLAKEFORGE_QUERY_TRINO_TLS_PORT="8443", OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP="olf-local-ca"
        ),
        stage="dev",
    )

    assert plain.sqlalchemy_uri == "trino://olf-dev-runtime@trino.olf-system:8080/lakehouse_dev"
    assert plain.extra is None
    assert tls.sqlalchemy_uri == "trino://olf-dev-runtime@trino.olf-system:8443/lakehouse_dev"
    assert tls.extra == {
        "engine_params": {"connect_args": {"http_scheme": "https", "verify": reports.SUPERSET_QUERY_CA}}
    }

    source = tmp_path / "report"
    (source / "databases").mkdir(parents=True)
    (source / "databases" / "trino.yaml").write_text("database_name: trino\nuuid: database-id\n")
    bundle_path = tmp_path / "bundle.zip"
    reports.build_report_bundle(source, bundle_path, "b", tls.sqlalchemy_uri, extra=tls.extra)
    with ZipFile(bundle_path) as bundle:
        database = yaml.safe_load(bundle.read("b/databases/trino.yaml"))
    assert database["extra"] == tls.extra


def test_bundle_schema_perms_lists_only_the_schemas_datasets_read(tmp_path: Path) -> None:
    write_report_bundle(tmp_path)
    (tmp_path / REPORT_DIR / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/iceberg\nuuid: {DATABASE_UUID}\n"
        "schema: not_a_dataset\n"
    )
    bundle_path = tmp_path / "bundle.zip"
    reports.build_report_bundle(
        tmp_path / REPORT_DIR, bundle_path, "b", "trino://x@trino:8080/iceberg", schema_prefix="lakehouse_dev_"
    )

    assert reports.bundle_schema_perms(bundle_path) == {"[Trino].[lakehouse_dev_order_revenue_gold]"}


def test_deploy_reports_grants_imported_schemas_once_and_keeps_other_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_superset: FakeSuperset
) -> None:
    write_report_bundle(tmp_path)
    _serve(monkeypatch, fake_superset)
    fake_superset.permissions.update(
        {1: ("schema_access", "[Trino].[order_revenue_gold]"), 2: ("schema_access", "[Trino].[other_silver]")}
    )
    fake_superset.roles["olf_data_access"] = {7}
    fake_superset.roles["Gamma"] = {8}

    for _ in range(2):
        superset.deploy_reports(
            tmp_path,
            "openlakeforge-dev",
            "trino://olf-dev@trino:8080/lakehouse_dev",
            report_source_dir=None,
            declared_report_dirs=(REPORT_DIR,),
            work_dir=tmp_path / "work",
            admin_username="admin",
            admin_password="secret",
        )

    assert fake_superset.roles == {"olf_data_access": {1, 7}, "Gamma": {8}}


def test_deploy_reports_without_sign_in_imports_and_grants_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_superset: FakeSuperset
) -> None:
    write_report_bundle(tmp_path)
    _serve(monkeypatch, fake_superset)
    fake_superset.roles.clear()

    superset.deploy_reports(
        tmp_path,
        "openlakeforge-dev",
        "trino://olf-dev@trino:8080/lakehouse_dev",
        report_source_dir=None,
        declared_report_dirs=(REPORT_DIR,),
        work_dir=tmp_path / "work",
        admin_username="admin",
        admin_password="secret",
    )

    assert len(fake_superset.imported) == 1
    assert fake_superset.roles == {}


def test_bundle_schema_perms_joins_datasets_to_databases_case_insensitively(tmp_path: Path) -> None:
    write_report_bundle(tmp_path, database_uuid=DATABASE_UUID.upper())
    dataset = tmp_path / REPORT_DIR / "datasets" / "mart.yaml"
    dataset.write_text(dataset.read_text().replace(DATABASE_UUID.upper(), DATABASE_UUID.lower()))
    bundle_path = tmp_path / "bundle.zip"
    reports.build_report_bundle(tmp_path / REPORT_DIR, bundle_path, "b", "trino://x@trino:8080/iceberg")

    assert reports.bundle_schema_perms(bundle_path) == {"[Trino].[order_revenue_gold]"}


def test_a_password_contract_names_the_stage_secret_the_import_reads_and_never_the_password() -> None:
    tls = {"OPENLAKEFORGE_QUERY_TRINO_TLS_PORT": "8443", "OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP": "olf-local-ca"}
    plain = superset.resolve_stage_report_target(_applied_environment(**tls), stage="dev")
    password = superset.resolve_stage_report_target(
        _applied_environment(**tls, OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET="trino-credential"), stage="dev"
    )

    assert plain.credential_secret == ""
    assert password.credential_secret == "trino-credential"
    # Superset's placeholder keeps the password the import's `passwords` field sets.
    assert password.sqlalchemy_uri == "trino://olf-dev-runtime:XXXXXXXXXX@trino.olf-system:8443/lakehouse_dev"
    assert plain.sqlalchemy_uri == "trino://olf-dev-runtime@trino.olf-system:8443/lakehouse_dev"
