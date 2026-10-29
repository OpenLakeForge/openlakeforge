from __future__ import annotations

import csv
from pathlib import Path

import pytest

from olf.bench import BenchError
from olf.bench._dataset import copy_project, generate, primary_key_columns


def _rows(project: Path, source: str, resource: str) -> list[dict[str, str]]:
    path = project / "lakehouse_code" / "bronze" / source / "examples" / f"{resource}.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_scale_one_reproduces_the_fixtures_byte_for_byte(external_project: Path, tmp_path: Path) -> None:
    fixture = (external_project / "lakehouse_code/bronze/crm/examples/orders.csv").read_bytes()
    project = copy_project(external_project, tmp_path / "scaled")

    tables = generate(project, scale=1)

    assert (project / "lakehouse_code/bronze/crm/examples/orders.csv").read_bytes() == fixture
    assert {(table.source, table.resource) for table in tables} >= {("crm", "orders"), ("erp", "shipments")}


def test_every_replica_is_a_disjoint_copy_whose_references_resolve(external_project: Path) -> None:
    fixture_lines = _rows(external_project, "crm", "order_lines")

    tables = {(table.source, table.resource): table for table in generate(external_project, scale=3)}

    lines = _rows(external_project, "crm", "order_lines")
    orders = {row["order_id"] for row in _rows(external_project, "crm", "orders")}
    products = {row["product_id"] for row in _rows(external_project, "crm", "products")}
    assert len(lines) == 3 * len(fixture_lines) == tables[("crm", "order_lines")].rows
    assert len({row["line_id"] for row in lines}) == len(lines)
    assert {row["order_id"] for row in lines} <= orders
    assert {row["product_id"] for row in lines} <= products
    assert any(row["line_id"].endswith("-R2") for row in lines)


def test_a_reference_named_unlike_its_key_keeps_pointing_at_the_first_replica(external_project: Path) -> None:
    generate(external_project, scale=2)

    accounts = {row["account_id"] for row in _rows(external_project, "crm", "accounts")}
    customers = {row["customer_id"] for row in _rows(external_project, "crm", "orders")}
    assert customers <= accounts
    assert not any(customer.endswith("-R1") for customer in customers)


def test_generation_is_deterministic(external_project: Path, tmp_path: Path) -> None:
    first = generate(copy_project(external_project, tmp_path / "a"), scale=4)
    second = generate(copy_project(external_project, tmp_path / "b"), scale=4)

    assert [table.sha256 for table in first] == [table.sha256 for table in second]


def test_primary_keys_come_from_the_floe_contracts(external_project: Path) -> None:
    keys = primary_key_columns(external_project / "lakehouse_code")

    assert keys[("crm", "order_lines")] == "line_id"
    assert keys[("erp", "purchase_orders")] == "purchase_order_id"


def test_a_composite_key_cannot_be_scaled(external_project: Path) -> None:
    contract = external_project / "lakehouse_code/silver/sales/contracts/floe/orders.yml"
    contract.write_text(
        contract.read_text().replace('primary_key: ["order_id"]', 'primary_key: ["order_id", "customer_id"]')
    )

    with pytest.raises(BenchError, match="single-column primary key"):
        generate(external_project, scale=2)


def test_a_non_string_key_cannot_be_suffixed(external_project: Path) -> None:
    contract = external_project / "lakehouse_code/silver/sales/contracts/floe/orders.yml"
    contract.write_text(
        contract.read_text().replace('{ name: "order_id", type: "string"', '{ name: "order_id", type: "integer"')
    )

    with pytest.raises(BenchError, match="must be a string column"):
        generate(external_project, scale=2)


def test_a_missing_fixture_fails(external_project: Path) -> None:
    (external_project / "lakehouse_code/bronze/erp/examples/shipments.csv").unlink()

    with pytest.raises(BenchError, match="no CSV fixture for erp/shipments"):
        generate(external_project, scale=1)


def test_scale_must_be_positive(external_project: Path) -> None:
    with pytest.raises(BenchError, match="at least 1"):
        generate(external_project, scale=0)
