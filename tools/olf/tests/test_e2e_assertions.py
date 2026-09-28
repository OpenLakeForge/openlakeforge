from pathlib import Path
from typing import Any

import pytest
from conftest import write_dashboard_fixture, write_two_product_fixture
from openlakeforge_domain import load_lakehouse_inventory

from olf.clients.openmetadata import OpenMetadataClient, OpenMetadataError
from olf.e2e import _assertions, _health
from olf.e2e._shell import E2EConfig, E2EError


def test_openmetadata_data_product_candidates_try_short_and_domain_names() -> None:
    seen: list[str] = []

    def request(method: str, path: str, **_kwargs) -> dict[str, Any]:
        seen.append(path)
        if path.endswith("/sales.sales_order_revenue"):
            return {}
        raise OpenMetadataError(f"{method} {path} failed with HTTP 404: not found")

    client = OpenMetadataClient("http://openmetadata")
    client.request = request

    assert _assertions._first_existing_data_product(client, ("sales_order_revenue", "sales.sales_order_revenue")) == (
        "sales.sales_order_revenue"
    )
    assert seen == [
        "/api/v1/dataProducts/name/sales_order_revenue",
        "/api/v1/dataProducts/name/sales.sales_order_revenue",
    ]


def test_two_product_fixture_repo_drives_exactly_its_own_jobs_dashboards_and_marts(tmp_path: Path) -> None:
    """A descriptor change alone must move e2e's discovered work, per issue #39."""
    write_two_product_fixture(
        tmp_path,
        dashboards=(
            ("widgets_alpha_overview", "widgets_alpha"),
            ("widgets_beta_main", "widgets_beta"),
            ("widgets_beta_detail", "widgets_beta"),
        ),
    )
    inventory = load_lakehouse_inventory(tmp_path)
    fixture_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=inventory,
    )

    assert fixture_cfg.inventory.job_names == ("widgets_alpha_pipeline", "widgets_beta_pipeline")
    assert fixture_cfg.inventory.gold_mart_names == (
        "widgets_alpha_gold.mart_alpha_summary",
        "widgets_beta_gold.mart_beta_summary",
    )

    # Dashboard identity comes from the exported Superset YAML, not the
    # descriptor's name — a title/slug that doesn't match that convention,
    # and a product with two dashboards, must both work.
    overview = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "widgets_alpha_overview")
    beta_main = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "widgets_beta_main")
    beta_detail = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "widgets_beta_detail")
    write_dashboard_fixture(
        tmp_path,
        overview.report_source_dir,
        "Overview_1.yaml",
        slug="widgets-alpha-overview",
        title="Widgets Alpha Overview Board",
    )
    write_dashboard_fixture(
        tmp_path, beta_main.report_source_dir, "Beta_Main_1.yaml", slug="widgets-beta-main", title="Widgets Beta"
    )
    write_dashboard_fixture(
        tmp_path, beta_detail.report_source_dir, "Beta_Detail_1.yaml", slug="widgets-beta-detail", title="Beta Detail"
    )

    expected = _assertions.discovered_dashboards(fixture_cfg)

    assert expected == {
        "widgets-alpha-overview": "Widgets Alpha Overview Board",
        "widgets-beta-main": "Widgets Beta",
        "widgets-beta-detail": "Beta Detail",
    }
    _assertions.assert_superset_dashboards(
        [
            {"slug": "widgets-alpha-overview", "dashboard_title": "Widgets Alpha Overview Board"},
            {"slug": "widgets-beta-main", "dashboard_title": "Widgets Beta"},
            {"slug": "widgets-beta-detail", "dashboard_title": "Beta Detail"},
        ],
        expected,
    )
    # Pipeline Job attempts are assessed by the launch-and-poll assertion, not
    # by the initial platform readiness check. Historical failed attempts must
    # not prevent a later suite from starting.
    assert _health.classify_pod_health(
        {
            "items": [
                {
                    "metadata": {
                        "name": "widgets-alpha-run-pod",
                        "ownerReferences": [{"kind": "Job", "name": "widgets_alpha_pipeline-run"}],
                    },
                    "status": {"phase": "Failed"},
                }
            ]
        }
    ) == []


def test_discovered_dashboards_rejects_a_product_with_no_dashboard_export(tmp_path: Path) -> None:
    """A missing dashboard export must fail loudly, not pass by contributing nothing."""
    write_two_product_fixture(
        tmp_path,
        dashboards=(
            ("widgets_alpha_overview", "widgets_alpha"),
            ("widgets_beta_overview", "widgets_beta"),
        ),
    )
    inventory = load_lakehouse_inventory(tmp_path)
    fixture_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=inventory,
    )
    # Only the alpha dashboard exports; the beta dashboard is declared but has none.
    overview = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "widgets_alpha_overview")
    write_dashboard_fixture(
        tmp_path,
        overview.report_source_dir,
        "Overview_1.yaml",
        slug="widgets-alpha-overview",
        title="Widgets Alpha Overview Board",
    )
    beta_overview = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "widgets_beta_overview")
    (tmp_path / beta_overview.report_source_dir).mkdir(parents=True)
    (tmp_path / beta_overview.report_source_dir / "metadata.yaml").write_text("type: assets\n", encoding="utf-8")

    with pytest.raises(E2EError, match="exports no Superset dashboards"):
        _assertions.discovered_dashboards(fixture_cfg)


def test_discovered_dashboards_accepts_yml_suffixed_exports(tmp_path: Path) -> None:
    """superset.build_report_bundle packages both .yaml and .yml — discovery must match."""
    write_two_product_fixture(
        tmp_path,
        dashboards=(
            ("widgets_alpha_overview", "widgets_alpha"),
            ("widgets_beta_main", "widgets_beta"),
        ),
    )
    inventory = load_lakehouse_inventory(tmp_path)
    fixture_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=inventory,
    )
    overview = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "widgets_alpha_overview")
    beta_main = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "widgets_beta_main")
    write_dashboard_fixture(
        tmp_path,
        overview.report_source_dir,
        "Overview_1.yaml",
        slug="widgets-alpha-overview",
        title="Widgets Alpha Overview Board",
    )
    write_dashboard_fixture(
        tmp_path, beta_main.report_source_dir, "Beta_Main_1.yml", slug="widgets-beta-main", title="Widgets Beta"
    )

    assert _assertions.discovered_dashboards(fixture_cfg) == {
        "widgets-alpha-overview": "Widgets Alpha Overview Board",
        "widgets-beta-main": "Widgets Beta",
    }


def test_discovered_dashboards_checks_every_dashboard_declared_for_a_product(tmp_path: Path) -> None:
    """Two dashboards on one product must both be checked, not just the first found."""
    lakehouse_dir = tmp_path / "lakehouse_code"
    source_dir = lakehouse_dir / "bronze" / "crm"
    source_dir.mkdir(parents=True)
    (source_dir / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: orders
""",
        encoding="utf-8",
    )
    (lakehouse_dir / "lakehouse.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - crm
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: planned
    silver_tables:
      tables:
        - {name: orders, source: crm, resource: orders}
    products:
      - id: orders
        displayName: Orders
        description: Orders product.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_orders
dashboards:
  - name: orders_overview
    products: [orders]
  - name: orders_detail
    products: [orders]
""",
        encoding="utf-8",
    )
    inventory = load_lakehouse_inventory(tmp_path)
    fixture_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=inventory,
    )
    overview = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "orders_overview")
    detail = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "orders_detail")
    write_dashboard_fixture(
        tmp_path, overview.report_source_dir, "Overview_1.yaml", slug="orders-overview", title="Orders Overview"
    )
    detail_dir = tmp_path / detail.report_source_dir
    detail_dir.mkdir(parents=True)
    (detail_dir / "metadata.yaml").write_text("type: assets\n", encoding="utf-8")

    # Only orders_overview exports a dashboard; orders_detail has none yet.
    with pytest.raises(E2EError, match="exports no Superset dashboards"):
        _assertions.discovered_dashboards(fixture_cfg)

    write_dashboard_fixture(
        tmp_path, detail.report_source_dir, "Detail_1.yaml", slug="orders-detail", title="Orders Detail"
    )

    assert _assertions.discovered_dashboards(fixture_cfg) == {
        "orders-overview": "Orders Overview",
        "orders-detail": "Orders Detail",
    }


def test_discovered_dashboards_skips_a_canonical_product_with_no_declared_dashboard(tmp_path: Path) -> None:
    """A product with zero dashboards in lakehouse.yaml is valid and must not be checked."""
    lakehouse_dir = tmp_path / "lakehouse_code"
    source_dir = lakehouse_dir / "bronze" / "crm"
    source_dir.mkdir(parents=True)
    (source_dir / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: orders
""",
        encoding="utf-8",
    )
    (lakehouse_dir / "lakehouse.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - crm
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: planned
    silver_tables:
      tables:
        - {name: orders, source: crm, resource: orders}
    products:
      - id: orders
        displayName: Orders
        description: Orders product.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_orders
      - id: internal_only
        displayName: Internal Only
        description: Product with no dashboard.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_internal
dashboards:
  - name: orders_overview
    products: [orders]
""",
        encoding="utf-8",
    )
    inventory = load_lakehouse_inventory(tmp_path)
    fixture_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=inventory,
    )
    overview = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "orders_overview")
    write_dashboard_fixture(
        tmp_path, overview.report_source_dir, "Overview_1.yaml", slug="orders-overview", title="Orders Overview"
    )

    # internal_only has no dashboard entry at all -- must not raise.
    assert _assertions.discovered_dashboards(fixture_cfg) == {
        "orders-overview": "Orders Overview",
    }


def test_discovered_dashboards_rejects_a_bundle_on_disk_with_no_declared_dashboard(tmp_path: Path) -> None:
    """superset.deploy_reports() discovers bundles from the filesystem, independent of
    lakehouse.yaml -- a directory that would still deploy must not be silently ignored."""
    lakehouse_dir = tmp_path / "lakehouse_code"
    source_dir = lakehouse_dir / "bronze" / "crm"
    source_dir.mkdir(parents=True)
    (source_dir / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: orders
""",
        encoding="utf-8",
    )
    (lakehouse_dir / "lakehouse.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - crm
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: planned
    silver_tables:
      tables:
        - {name: orders, source: crm, resource: orders}
    products:
      - id: orders
        displayName: Orders
        description: Orders product.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_orders
dashboards:
  - name: orders_overview
    products: [orders]
""",
        encoding="utf-8",
    )
    inventory = load_lakehouse_inventory(tmp_path)
    fixture_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=inventory,
    )
    overview = next(dashboard for dashboard in inventory.dashboards if dashboard.name == "orders_overview")
    write_dashboard_fixture(
        tmp_path, overview.report_source_dir, "Overview_1.yaml", slug="orders-overview", title="Orders Overview"
    )
    (tmp_path / overview.report_source_dir / "metadata.yaml").write_text("type: assets\n", encoding="utf-8")

    # A directory exists that deploy_reports() would discover and import, but
    # no dashboards[] entry in lakehouse.yaml references it.
    rogue_dir = lakehouse_dir / "dashboards" / "superset" / "rogue"
    (rogue_dir / "dashboards").mkdir(parents=True)
    (rogue_dir / "metadata.yaml").write_text("type: assets\n", encoding="utf-8")
    (rogue_dir / "dashboards" / "Rogue_1.yaml").write_text(
        "dashboard_title: Rogue\nslug: rogue\n", encoding="utf-8"
    )

    with pytest.raises(E2EError, match="lakehouse_code/dashboards/superset/rogue"):
        _assertions.discovered_dashboards(fixture_cfg)


def _registered(*paths: str) -> OpenMetadataClient:
    """An OpenMetadata that answers 200 for `paths` and 404 for anything else."""
    client = OpenMetadataClient("http://openmetadata")

    def request(method: str, path: str, *, ok_statuses: tuple[int, ...] = (200,), **_kwargs) -> dict[str, Any]:
        status = 200 if path in paths else 404
        if status not in ok_statuses:
            raise OpenMetadataError(f"{method} {path} failed with HTTP {status}")
        return {}

    client.request = request  # type: ignore[method-assign]
    return client


_PROD_ROOTS = (
    "/api/v1/databases/name/polaris.lakehouse_prod",
    "/api/v1/services/pipelineServices/name/dagster_prod",
    "/api/v1/services/dashboardServices/name/superset_prod",
)
_PROD_CONTRACT = {
    "orchestration": {"pipeline_service_name": "dagster_prod"},
    "reporting": {"dashboard_service_name": "superset_prod"},
}


def test_stage_roots_accept_a_stage_registered_under_its_own_names() -> None:
    _assertions.assert_openmetadata_stage_roots(
        _registered(*_PROD_ROOTS), stage="prod", stage_contract=_PROD_CONTRACT, database_fqn="polaris.lakehouse_prod"
    )


@pytest.mark.parametrize("missing", _PROD_ROOTS)
def test_stage_roots_reject_a_missing_root(missing: str) -> None:
    client = _registered(*(path for path in _PROD_ROOTS if path != missing))

    with pytest.raises(E2EError, match="missing stage prod's"):
        _assertions.assert_openmetadata_stage_roots(
            client, stage="prod", stage_contract=_PROD_CONTRACT, database_fqn="polaris.lakehouse_prod"
        )


def test_stage_roots_need_no_dashboard_service_without_analytics() -> None:
    _assertions.assert_openmetadata_stage_roots(
        _registered(*_PROD_ROOTS[:2]),
        stage="prod",
        stage_contract={"orchestration": _PROD_CONTRACT["orchestration"]},
        database_fqn="polaris.lakehouse_prod",
    )


def test_openmetadata_check_skips_stage_roots_for_a_v2_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pre-stage (v2) contract has no stage index; its asset checks still run."""
    from contextlib import contextmanager
    from dataclasses import replace

    from conftest import e2e_cfg

    @contextmanager
    def _port_forward(*_args, **_kwargs):  # noqa: ANN202
        yield 18585

    checked: list[str] = []
    monkeypatch.setattr(_assertions.k8s, "port_forward", _port_forward)
    monkeypatch.setattr(_assertions.k8s, "http_wait", lambda *_a, **_k: True)
    monkeypatch.setattr(_assertions, "assert_openmetadata_assets", lambda *_a: checked.append("assets"))
    monkeypatch.setattr(_assertions, "load_provider_contracts_or_raise", lambda _cfg: {"version": 2})
    monkeypatch.setattr(
        _assertions, "assert_openmetadata_stage_roots", lambda *_a, **_k: pytest.fail("v2 has no stage roots")
    )

    _assertions.check_openmetadata_assets(replace(e2e_cfg(tmp_path), openmetadata_local_port=18585))

    assert checked == ["assets"]


def test_stage_roots_reject_a_leftover_unqualified_service() -> None:
    client = _registered(*_PROD_ROOTS, "/api/v1/services/pipelineServices/name/dagster")

    with pytest.raises(E2EError, match="unqualified pre-stage service is still registered"):
        _assertions.assert_openmetadata_stage_roots(
            client, stage="prod", stage_contract=_PROD_CONTRACT, database_fqn="polaris.lakehouse_prod"
        )
