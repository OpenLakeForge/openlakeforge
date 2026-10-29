from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from olf.bench import BenchError
from olf.bench._pipeline import column_expression, layer_of, run_job, summarize_run, table_facts
from olf.clients.base import ServiceClientError


def test_layer_of_follows_the_product_definition_step_names() -> None:
    assert layer_of("crm_bronze_resources") == "bronze"
    assert layer_of("order_revenue_dbt_gold_assets") == "gold"
    assert layer_of("openlakeforge__sales") == "silver"


def _stats() -> dict[str, Any]:
    return {
        "status": "SUCCESS",
        "startTime": 100.0,
        "endTime": 190.0,
        "stepStats": [
            {"stepKey": "crm_bronze_resources", "status": "SUCCESS", "startTime": 101.0, "endTime": 111.0},
            {"stepKey": "erp_bronze_resources", "status": "SUCCESS", "startTime": 102.0, "endTime": 115.0},
            {"stepKey": "openlakeforge__sales", "status": "SUCCESS", "startTime": 116.0, "endTime": 160.0},
            {"stepKey": "order_revenue_dbt_gold_assets", "status": "SUCCESS", "startTime": 161.0, "endTime": None},
        ],
    }


def test_summarize_run_reports_each_layer_as_its_wall_clock_span() -> None:
    summary = summarize_run(_stats())

    assert summary["seconds"] == 90.0
    assert summary["layers"]["bronze"] == {"start": 101.0, "end": 115.0, "seconds": 14.0, "steps": 2}
    assert summary["layers"]["silver"]["seconds"] == 44.0
    assert summary["layers"]["gold"]["seconds"] is None
    assert [step["layer"] for step in summary["steps"]] == ["bronze", "bronze", "silver", "gold"]


class _Client:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail

    def launch(self, job: str) -> str:
        return f"run-{job}"

    def poll(self, job: str, run_id: str, *, timeout_seconds: int) -> None:
        if self.fail:
            raise ServiceClientError(f"{job} run {run_id} ended with FAILURE")

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"runOrError": {"__typename": "Run", **_stats()}}


def test_run_job_records_the_run_and_its_steps() -> None:
    detail: dict[str, Any] = {}

    run_job(_Client(fail=False), "order_revenue_pipeline", detail, timeout_seconds=60)  # type: ignore[arg-type]

    assert detail["run_id"] == "run-order_revenue_pipeline"
    assert detail["layers"]["silver"]["steps"] == 1


def test_a_failed_run_still_records_its_steps_before_failing() -> None:
    detail: dict[str, Any] = {}

    with pytest.raises(BenchError, match="FAILURE"):
        run_job(_Client(fail=True), "order_revenue_pipeline", detail, timeout_seconds=60)  # type: ignore[arg-type]

    assert len(detail["steps"]) == 4


def test_floating_point_columns_are_rounded_for_the_checksum() -> None:
    assert column_expression("arr", "double") == "format('%.6f', \"arr\")"
    assert column_expression("order_id", "varchar") == 'cast("order_id" as varchar)'


def test_table_facts_reads_rows_checksum_files_and_snapshots() -> None:
    answers = {
        "information_schema": "account_id,varchar\narr,decimal(12,2)\n",
        "checksum(": "24,0A1B\n",
        "$files": "3,4096\n",
        "$snapshots": "2\n",
    }
    sent: list[str] = []

    def query(sql: str) -> str:
        sent.append(sql)
        return next(answer for marker, answer in answers.items() if marker in sql)

    facts = table_facts(query, "olf_dev", "sales_silver", "accounts")

    assert facts == {"rows": 24, "checksum": "0A1B", "data_files": 3, "data_file_bytes": 4096, "snapshots": 2}
    assert 'cast("arr" as varchar)' in sent[1]


def test_table_facts_requires_columns() -> None:
    with pytest.raises(BenchError, match="no columns"):
        table_facts(lambda _sql: "", "olf_dev", "sales_silver", "missing")


def test_table_facts_rejects_a_malformed_answer() -> None:
    def query(sql: str) -> str:
        return "id,varchar\n" if "information_schema" in sql else "not a row\n"

    with pytest.raises(BenchError, match="expected one row"):
        table_facts(query, "olf_dev", "sales_silver", "accounts")
