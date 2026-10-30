from __future__ import annotations

from pathlib import Path

import pytest
from openlakeforge_domain import CatalogNamespace

from olf import catalog

REPO_ROOT = Path(__file__).resolve().parents[3]


def ns(name: str, location: str) -> CatalogNamespace:
    return CatalogNamespace(name=name, location=location)


def test_plan_creates_namespaces_absent_from_the_catalog() -> None:
    plan = catalog.plan_namespace_sync({}, [ns("sales_silver", "s3://silver/sales_silver/")])

    assert [namespace.name for namespace in plan.create] == ["sales_silver"]
    assert plan.update == ()
    assert plan.delete == ()
    assert not plan.is_empty


def test_plan_leaves_matching_namespaces_alone() -> None:
    desired = [ns("sales_silver", "s3://silver/sales_silver/")]
    existing = {
        "sales_silver": catalog.NamespaceState(
            "s3://silver/sales_silver/", {catalog.MANAGED_BY_KEY: catalog.MANAGED_BY_VALUE}
        )
    }
    plan = catalog.plan_namespace_sync(existing, desired)

    assert plan.create == ()
    assert plan.update == ()
    assert plan.unchanged == ("sales_silver",)
    assert plan.is_empty


def test_plan_relocates_a_namespace_whose_location_drifted() -> None:
    desired = [ns("sales_silver", "s3://new-silver/sales_silver/")]
    existing = {
        "sales_silver": catalog.NamespaceState(
            "s3://old-silver/sales_silver/", {catalog.MANAGED_BY_KEY: catalog.MANAGED_BY_VALUE}
        )
    }
    plan = catalog.plan_namespace_sync(existing, desired)

    assert plan.create == ()
    assert [namespace.location for namespace in plan.update] == ["s3://new-silver/sales_silver/"]


def test_plan_reports_orphans_but_keeps_them_without_prune() -> None:
    existing = {
        "retired_gold": catalog.NamespaceState(
            "s3://gold/retired_gold/", {catalog.MANAGED_BY_KEY: catalog.MANAGED_BY_VALUE}
        )
    }
    plan = catalog.plan_namespace_sync(existing, [])

    assert plan.orphans == ("retired_gold",)
    assert plan.delete == ()
    assert plan.is_empty
    assert "undeclared managed retired_gold" in catalog.render_plan(plan, prune=False)


def test_plan_deletes_orphans_only_when_pruning() -> None:
    existing = {
        "retired_gold": catalog.NamespaceState(
            "s3://gold/retired_gold/", {catalog.MANAGED_BY_KEY: catalog.MANAGED_BY_VALUE}
        )
    }
    plan = catalog.plan_namespace_sync(existing, [], prune=True)

    assert plan.delete == ("retired_gold",)
    assert not plan.is_empty
    assert "- remove metadata retired_gold" in catalog.render_plan(plan, prune=True)


def test_plan_never_prunes_a_foreign_undeclared_namespace() -> None:
    plan = catalog.plan_namespace_sync({"shared": "s3://someone-else/shared/"}, [], prune=True)

    assert plan.delete == ()
    assert plan.foreign == ("shared",)


def test_plan_adopts_only_location_matching_legacy_namespace() -> None:
    plan = catalog.plan_namespace_sync(
        {"sales_silver": "s3://silver/sales_silver/"}, [ns("sales_silver", "s3://silver/sales_silver/")]
    )
    assert [item.name for item in plan.adopt] == ["sales_silver"]


def test_plan_refuses_to_relocate_foreign_namespace() -> None:
    with pytest.raises(catalog.NamespaceSyncError, match="not managed"):
        catalog.plan_namespace_sync(
            {"sales_silver": "s3://other/sales_silver/"}, [ns("sales_silver", "s3://silver/sales_silver/")]
        )


def test_plan_rejects_legacy_product_silver_namespace_with_reset_guidance() -> None:
    desired = [ns("sales_silver", "s3://silver/sales_silver/")]

    with pytest.raises(catalog.NamespaceSyncError, match="reset-only.*destroy and recreate"):
        catalog.plan_namespace_sync(
            {"order_revenue_silver": "s3://silver/order_revenue_silver/"},
            desired,
        )


def test_plan_ignores_a_sibling_stages_canonical_silver_namespace() -> None:
    """AWS Glue's shared default catalog surfaces every enabled stage's own
    namespaces in `existing`, each carrying its own `namespace_prefix`
    (lakehouse_<stage>_). Reconciling one stage must not treat another
    already-provisioned stage's canonical names as legacy drift."""
    desired = [ns("lakehouse_prod_sales_silver", "s3://silver/lakehouse_prod_sales_silver/")]

    plan = catalog.plan_namespace_sync(
        {
            "lakehouse_dev_sales_silver": catalog.NamespaceState(
                "s3://silver/lakehouse_dev_sales_silver/", {catalog.MANAGED_BY_KEY: catalog.MANAGED_BY_VALUE}
            )
        },
        desired,
        prune=True,
        namespace_prefix="lakehouse_prod_",
    )

    assert [namespace.name for namespace in plan.create] == ["lakehouse_prod_sales_silver"]
    assert plan.orphans == ()
    assert plan.delete == ()


def test_plan_rejects_unprefixed_v02_glue_namespace_before_creating_a_parallel_stage_namespace() -> None:
    desired = [ns("lakehouse_dev_sales_silver", "s3://silver/lakehouse_dev_sales_silver/")]

    with pytest.raises(catalog.NamespaceSyncError, match="unprefixed v0.2 Glue.*Iceberg table registrations"):
        catalog.plan_namespace_sync(
            {"sales_silver": "s3://silver/sales_silver/"},
            desired,
            namespace_prefix="lakehouse_dev_",
        )


def test_plan_still_rejects_a_same_prefix_legacy_silver_namespace() -> None:
    desired = [ns("lakehouse_prod_sales_silver", "s3://silver/lakehouse_prod_sales_silver/")]

    with pytest.raises(catalog.NamespaceSyncError, match="reset-only.*destroy and recreate"):
        catalog.plan_namespace_sync(
            {"lakehouse_prod_order_revenue_silver": "s3://silver/lakehouse_prod_order_revenue_silver/"},
            desired,
            namespace_prefix="lakehouse_prod_",
        )


def test_desired_namespaces_follow_the_repository_descriptors() -> None:
    namespaces = catalog.desired_namespaces(
        REPO_ROOT, bronze_bucket="lakehouse-bronze", silver_bucket="lakehouse-silver", gold_bucket="lakehouse-gold"
    )

    by_name = {namespace.name: namespace.location for namespace in namespaces}
    assert by_name["crm_bronze"] == "s3://lakehouse-bronze/crm_bronze/"
    assert by_name["erp_bronze"] == "s3://lakehouse-bronze/erp_bronze/"
    assert by_name["sales_silver"] == "s3://lakehouse-silver/sales_silver/"
    assert by_name["supply_chain_silver"] == "s3://lakehouse-silver/supply_chain_silver/"
    assert by_name["order_revenue_gold"] == "s3://lakehouse-gold/order_revenue_gold/"
    assert "customer_health_gold" in by_name
    assert "inventory_reliability_gold" in by_name


def test_desired_namespaces_honour_custom_buckets() -> None:
    namespaces = catalog.desired_namespaces(
        REPO_ROOT, bronze_bucket="poc-bronze", silver_bucket="poc-silver", gold_bucket="poc-gold"
    )

    locations = {namespace.location for namespace in namespaces}
    assert all(
        location.startswith(("s3://poc-bronze/", "s3://poc-silver/", "s3://poc-gold/")) for location in locations
    )


class FakeClient:
    """Records the reconciliation calls apply_namespace_sync makes.

    Deliberately provider-agnostic: this is the NamespaceClient protocol both
    olf.polaris.PolarisClient and olf.glue.GlueClient implement.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_namespace(self, namespace: CatalogNamespace) -> None:
        self.calls.append(("create", namespace.name))

    def update_namespace_location(self, namespace: CatalogNamespace) -> None:
        self.calls.append(("update", namespace.name))

    def adopt_namespace(self, namespace: CatalogNamespace) -> None:
        self.calls.append(("adopt", namespace.name))

    def drop_namespace(self, name: str) -> None:
        self.calls.append(("drop", name))


def test_apply_creates_before_it_drops() -> None:
    plan = catalog.NamespaceSyncPlan(
        create=(ns("new_silver", "s3://silver/new_silver/"),),
        update=(ns("moved_gold", "s3://gold/moved_gold/"),),
        delete=("retired_silver",),
    )
    client = FakeClient()

    catalog.apply_namespace_sync(client, plan)

    assert client.calls == [
        ("create", "new_silver"),
        ("update", "moved_gold"),
        ("drop", "retired_silver"),
    ]
