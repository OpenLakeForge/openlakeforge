"""The committed OpenAPI document matches the app, and the typed responses keep the JSON clients already read."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openlakeforge_services.dagster import DagsterJob, DagsterRun
from openlakeforge_services.logs import ArchivedLogLine
from openlakeforge_services.openmetadata import DataProduct as ServiceDataProduct

from openlakeforge_platform_api import models
from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.openapi import DOCUMENT_PATH, REGENERATE, render
from openlakeforge_platform_api.settings import Settings

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def test_the_committed_document_is_what_the_app_renders() -> None:
    committed = DOCUMENT_PATH.read_text(encoding="utf-8") if DOCUMENT_PATH.exists() else ""

    assert committed == render(), (
        f"{DOCUMENT_PATH.name} is out of date with the platform API's routes or response models; "
        f"regenerate it with: {REGENERATE}"
    )


def _operations() -> dict[tuple[str, str], dict[str, Any]]:
    paths = json.loads(render())["paths"]
    return {(method.upper(), path): op for path, item in paths.items() for method, op in item.items()}


def test_the_document_lists_every_route_the_app_serves(settings: Settings) -> None:
    app = create_app(settings)
    served = {
        (method, route.path)  # type: ignore[attr-defined]
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
    }

    assert set(_operations()) == served
    assert len(served) == 18


def test_every_route_answers_with_a_typed_or_declared_body() -> None:
    for (method, path), operation in _operations().items():
        ok = next(response for status, response in operation["responses"].items() if status.startswith("2"))
        content = ok["content"]
        assert content, f"{method} {path} declares no response body"
        for media in content.values():
            assert media["schema"], f"{method} {path} declares an empty schema"


def test_the_app_still_serves_no_document_or_docs_route(client: TestClient) -> None:
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code in (403, 404)


def _wire(payload: Any) -> bytes:
    return JSONResponse(payload).body


def _typed(model: Any) -> bytes:
    # FastAPI serializes a response model through jsonable_encoder before JSONResponse.
    return _wire(jsonable_encoder(model))


def test_a_run_keeps_its_wire_form_whole_second_times_and_missing_times_included() -> None:
    runs = [
        DagsterRun("r1", "orders_job", "SUCCESS", 1761800000.5, 1761800001, 1761800090.25, {"a": "b"}),
        DagsterRun("r2", "orders_job", "QUEUED", 1761800100.0, None, None),
    ]

    legacy = _wire({"runs": [asdict(run) for run in runs]})

    assert _typed(models.Runs(runs=[models.Run(**asdict(run)) for run in runs])) == legacy


def test_jobs_log_lines_and_data_products_keep_their_wire_form() -> None:
    job = DagsterJob("loc", "repo", "orders_job", "")
    lines = [
        ArchivedLogLine("2026-11-01T00:00:00Z", "dev", "pod-1", "main", line="hello"),
        ArchivedLogLine("2026-11-01T00:00:01Z", "dev", "pod-1", "main", error="unreadable"),
    ]
    product = ServiceDataProduct("orders", "Orders", ("sales", "finance"))

    assert _typed(models.Jobs(jobs=[models.Job(**asdict(job))])) == _wire({"jobs": [asdict(job)]})
    assert _typed(models.Logs(lines=[models.LogLine(**asdict(line)) for line in lines], truncated=True)) == _wire(
        {"lines": [asdict(line) for line in lines], "truncated": True}
    )
    assert _typed(models.DataProducts(data_products=[models.DataProduct(**asdict(product))])) == _wire(
        {"data_products": [asdict(product)]}
    )


@pytest.mark.parametrize("cursor", [None, "abc"])
def test_a_query_result_keeps_its_wire_form_with_mixed_cell_types(cursor: str | None) -> None:
    rows: list[list[Any]] = [[1, "a", None, 2.5, True], [2, "b", {"k": [1]}, 0.0, False]]
    legacy = {
        "id": "id-1",
        "status": "running",
        "columns": [{"name": "n", "type": "integer"}, {"name": "s", "type": "varchar"}],
        "rows": rows,
        "truncated": False,
        "row_count": 2,
        "elapsed_ms": 7,
        "next_cursor": cursor,
    }

    typed = models.QueryResult(
        id="id-1",
        status="running",
        columns=[models.QueryColumn(name="n", type="integer"), models.QueryColumn(name="s", type="varchar")],
        rows=rows,
        truncated=False,
        row_count=2,
        elapsed_ms=7,
        next_cursor=cursor,
    )

    assert _typed(typed) == _wire(legacy)
