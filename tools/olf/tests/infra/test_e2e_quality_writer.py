"""The e2e quality-writer check's verdicts, against a scripted OpenMetadata (the live check runs in the Full suite)."""

from __future__ import annotations

from typing import Any

import pytest
from openlakeforge_services.openmetadata import OpenMetadataError

from olf.infra.e2e._quality_writer import _expect_refused, _publish
from olf.infra.e2e._shell import E2EError

SILVER = "polaris.lakehouse_dev.sales_silver.orders"
GOLD = "polaris.lakehouse_dev.revenue_gold.daily"


class Catalog:
    """Answers a request with the status `answers` names for its method and path prefix, else as 1.13.6 would."""

    def __init__(self, answers: dict[tuple[str, str], int] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str, Any]] = []
        self.results: list[dict[str, Any]] = []

    def request(self, method: str, path: str, *, ok_statuses: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs.get("json_body")))
        status = next((s for (m, p), s in self.answers.items() if m == method and path.startswith(p)), None)
        if status is None:
            status = ok_statuses[0]
        if status not in ok_statuses:
            raise OpenMetadataError(f"{method} {path} failed with HTTP {status}", status=status)
        if path.startswith("/api/v1/tables/name/"):
            fqn = SILVER if "orders" in path else GOLD
            return {
                "id": f"id-{fqn.rsplit('.', 1)[1]}",
                "fullyQualifiedName": fqn,
                "description": "kept",
                "columns": [],
            }
        if path == "/api/v1/dataQuality/testCases":
            return {"fullyQualifiedName": f'{SILVER}."{kwargs["json_body"]["name"]}"'}
        if path.startswith("/api/v1/dataQuality/testCases/testCaseResults/"):
            if method == "POST":
                self.results.append(kwargs["json_body"])
            return {"data": self.results}
        return {}


def test_a_refused_writer_passes_and_nothing_it_sent_would_change_the_table() -> None:
    writer = Catalog({("PATCH", "/api/v1/tables/"): 403, ("PUT", "/api/v1/lineage"): 403})

    _expect_refused(writer, Catalog(), SILVER, GOLD)  # type: ignore[arg-type]

    patch = next(body for method, _, body in writer.calls if method == "PATCH")
    assert patch == [{"op": "add", "path": "/description", "value": "kept"}]
    edge = next(body for method, _, body in writer.calls if method == "PUT")["edge"]
    assert (edge["fromEntity"]["id"], edge["toEntity"]["id"]) == ("id-daily", "id-orders")


def test_a_writer_that_may_edit_a_description_fails_the_check() -> None:
    writer = Catalog({("PATCH", "/api/v1/tables/"): 200, ("PUT", "/api/v1/lineage"): 403})

    with pytest.raises(E2EError, match="description"):
        _expect_refused(writer, Catalog(), SILVER, GOLD)  # type: ignore[arg-type]


def test_a_writer_that_may_add_lineage_fails_the_check_and_its_edge_is_removed() -> None:
    writer = Catalog({("PATCH", "/api/v1/tables/"): 403, ("PUT", "/api/v1/lineage"): 200})
    admin = Catalog()

    with pytest.raises(E2EError, match="lineage"):
        _expect_refused(writer, admin, SILVER, GOLD)  # type: ignore[arg-type]

    assert admin.calls == [("DELETE", "/api/v1/lineage/table/id-daily/table/id-orders", None)]


def test_a_writer_that_can_publish_returns_the_case_it_wrote_to() -> None:
    writer = Catalog()

    case = _publish(writer, "dev", SILVER)  # type: ignore[arg-type]

    assert case == f'{SILVER}."olf.floe.olf_e2e_writer_rule"'
    assert [r["testCaseStatus"] for r in writer.results] == ["Success"]


def test_a_writer_refused_a_result_fails_the_check() -> None:
    writer = Catalog({("POST", "/api/v1/dataQuality/testCases/testCaseResults/"): 403})

    with pytest.raises(E2EError, match="could not publish"):
        _publish(writer, "dev", SILVER)  # type: ignore[arg-type]
