from openlakeforge_services import governance


def _domain(**overrides: object) -> governance.DomainSpec:
    fields = {
        "name": "sales",
        "display_name": "Sales",
        "description": "Sales domain",
        "status": "active",
        "silver_tables": ("orders",),
        "bronze": (),
        "products": (),
    }
    return governance.DomainSpec(**{**fields, **overrides})


def test_domain_payload_carries_the_governance_text_and_status() -> None:
    payload = governance.domain_payload(_domain())

    assert payload == {
        "name": "sales",
        "displayName": "Sales",
        "domainType": "Source-aligned",
        "description": "Sales domain\nStatus: active",
    }


def test_a_domain_without_description_or_status_has_an_empty_description() -> None:
    assert governance.domain_payload(_domain(description="", status=""))["description"] == ""


def test_product_payload_names_its_domain() -> None:
    product = governance.ProductSpec(
        id="order_revenue",
        display_name="Sales Order Revenue",
        description="Revenue from orders.",
        domain="sales",
        gold_tables=("mart_order_revenue_by_day",),
    )

    assert governance.product_payload(product) == {
        "name": "order_revenue",
        "displayName": "Sales Order Revenue",
        "description": "Revenue from orders.",
        "domains": ["sales"],
    }
