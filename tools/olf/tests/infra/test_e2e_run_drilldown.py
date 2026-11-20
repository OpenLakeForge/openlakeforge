from __future__ import annotations

import contextlib
import json
import re
import urllib.parse
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import requests
from conftest import E2E_INVENTORY, e2e_cfg
from openlakeforge_services.dagster import DagsterRun

from olf.infra.e2e import _run_drilldown as drill
from olf.infra.e2e._metrics import ApiSession
from olf.infra.e2e._shell import E2EError

PRODUCT = E2E_INVENTORY.default_product
SECOND = E2E_INVENTORY.products[1]
RUN_ID = "0f6c3a52-8d3e-4c4f-9a65-2b7f1c9d0e11"
STAGE = "lakehouse"
START, END = 1_763_000_000.0, 1_763_000_600.0
REPORT_ID = "0123456789abcdef"
REPORT_HREF = f"/api/v1/stages/{STAGE}/runs/{RUN_ID}/quality/reports/{REPORT_ID}"
PODS = ("order-revenue-run-abc", "floe-sales-xyz")


def _iso(seconds: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def _run(**changes: Any) -> DagsterRun:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "job_name": PRODUCT.job_name,
        "status": "FAILURE",
        "created_at": START - 5,
        "started_at": START,
        "ended_at": END,
        "tags": {drill.DRILL_TAG: "failed-run"},
    }
    return DagsterRun(**{**values, **changes})


def _lines(count: int = 450) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": _iso(START + 1 + n),
            "namespace": "lakehouse",
            "pod": PODS[n % 2],
            "container": "main",
            "stream": "stderr",
            "line": f"line {n}",
            "level": "error" if n % 10 == 0 else None,
            "run_id": RUN_ID,
        }
        for n in range(count)
    ]


def _check(check_id: str, outcome: str, engine: str = "floe") -> dict[str, Any]:
    return {"engine": engine, "check_id": check_id, "outcome": outcome, "report_id": REPORT_ID}


def _counts(**values: int) -> dict[str, int]:
    return {"passed": 0, "warn": 0, "error": 0, "not_run": 0, "unknown": 0, "blocking": 0, **values}


class FakeApi:
    """The platform API's run routes over one failed run, paging its logs by an opaque index cursor."""

    def __init__(self, lines: list[dict[str, Any]] | None = None, refuse_logs: bool = True) -> None:
        self.lines = _lines() if lines is None else lines
        self.refuse_logs = refuse_logs
        self.failure = {
            "run_id": RUN_ID,
            "job": PRODUCT.job_name,
            "domain": PRODUCT.domain_name,
            "product": PRODUCT.id,
            "status": "FAILURE",
            "steps": [{"step": f"{PRODUCT.id}_dbt_gold_assets", "message": "ValueError"}],
            "sources": [{"name": "dagster", "status": "available"}, {"name": "quality", "status": "available"}],
        }
        self.quality: dict[str, Any] = {
            "run_id": RUN_ID,
            "complete": False,
            "checks": [_check("floe_not_null", "passed"), _check("floe_cast_error", "warn")],
            "counts": _counts(passed=1, warn=1),
            "reports": [{"id": REPORT_ID, "engine": "floe", "table": "orders", "href": REPORT_HREF}],
        }
        self.report = (b'{"entity": "orders"}', "application/json")
        self.calls: list[tuple[str, str]] = []

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> requests.Response:
        reply = requests.Response()
        reply.status_code = status
        reply._content = body
        reply.headers["Content-Type"] = content_type
        return reply

    def get(self, path: str, role: str) -> requests.Response:
        self.calls.append((role, path))
        if role == "analyst":
            return self._reply(403, b'{"detail": "role analyst may not call it"}')
        parts = urllib.parse.urlsplit(path)
        base = f"/api/v1/stages/{STAGE}/runs/{RUN_ID}"
        if parts.path == f"{base}/failure":
            return self._reply(200, json.dumps(self.failure).encode())
        if parts.path == f"{base}/quality":
            return self._reply(200, json.dumps(self.quality).encode())
        if parts.path == REPORT_HREF:
            return self._reply(200, *self.report)
        if parts.path == f"{base}/logs":
            query = dict(urllib.parse.parse_qsl(parts.query))
            start, limit = int(query.get("after", "0")), int(query["limit"])
            page = self.lines[start : start + limit]
            following = str(start + limit) if start + limit < len(self.lines) else None
            body = {
                "lines": page,
                "truncated": following is not None,
                "next": following,
                "since": _iso(START) if start == 0 else page[0]["timestamp"],
                "until": _iso(END),
            }
            return self._reply(200, json.dumps(body).encode())
        return self._reply(404, b"{}")

    def session(self, refused: str | None = "analyst") -> ApiSession:
        session = ApiSession(route=None, base_url="", token=lambda _role: "", reader="data-engineer", refused=refused)  # type: ignore[arg-type]
        session.get = self.get  # type: ignore[method-assign]
        return session


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


def test_the_selection_leaves_out_bronze_by_source_name() -> None:
    keys = [["crm", "customers"], ["sales", "orders"], ["order_revenue", "daily_revenue"], ["erp", "orders"]]
    assert drill.drilldown_selection(keys, ["crm", "erp"]) == [["sales", "orders"], ["order_revenue", "daily_revenue"]]


def test_a_failure_must_name_the_product_scope_and_a_step(api: FakeApi) -> None:
    assert drill.failure_problems(api.failure, PRODUCT) == []
    other = {**api.failure, "domain": None, "product": None, "steps": [], "status": "SUCCESS"}
    problems = drill.failure_problems(other, PRODUCT)
    assert any("not FAILURE" in p for p in problems)
    assert any("scope is None/None" in p for p in problems)
    assert "names no failed step" in problems


def test_a_warn_counted_as_an_error_is_a_problem(api: FakeApi) -> None:
    assert drill.quality_problems(api.quality) == []
    swapped = {**api.quality, "counts": _counts(passed=1, error=1)}
    assert drill.quality_problems(swapped) == [
        "warn is counted 0, the checks hold 1",
        "error is counted 1, the checks hold 0",
    ]
    assert drill.quality_problems({**api.quality, "checks": None, "sources": []}) == ["no checks: []"]


@pytest.mark.parametrize(
    ("href", "problem"),
    [
        (None, "no report has an href"),
        ("https://seaweedfs.olf.localhost/ops/report.json", "names an origin"),
        (f"{REPORT_HREF}?X-Amz-Signature=abc", "query string"),
        (f"/api/v1/stages/prod/runs/{RUN_ID}/quality/reports/{REPORT_ID}", "not under the run's report route"),
    ],
)
def test_a_report_link_must_be_the_apis_own_path(href: str | None, problem: str) -> None:
    assert drill.report_href_problems(REPORT_HREF, STAGE, RUN_ID) == []
    assert any(problem in p for p in drill.report_href_problems(href, STAGE, RUN_ID))


def test_nanosecond_times_parse_as_utc() -> None:
    assert drill.parse_time("2026-11-20T10:00:00.123456789Z").microsecond == 123456
    assert drill.parse_time("2026-11-20T10:00:00").tzinfo is not None


def test_the_log_window_is_read_page_by_page_until_next_is_null(api: FakeApi) -> None:
    read = drill.read_run_logs(
        api.session().read, f"/api/v1/stages/{STAGE}/runs/{RUN_ID}/logs?match_run=true&limit=200"
    )
    assert (read.pages, len(read.lines)) == (3, 450)
    assert read.since == _iso(START)
    assert drill.log_problems(read, _run()) == []
    assert drill.missing_pods(read, [*PODS, "gone-before-lookup"]) == ["gone-before-lookup"]


def test_a_cursor_that_comes_back_would_loop_and_fails() -> None:
    page = {"lines": [], "truncated": True, "next": "same", "since": _iso(START), "until": _iso(END)}
    with pytest.raises(E2EError, match="cursor an earlier page returned"):
        drill.read_run_logs(lambda _path: page, "/logs?limit=1")


def test_a_page_whose_truncated_flag_disagrees_with_next_fails() -> None:
    page = {"lines": [], "truncated": True, "next": None, "since": _iso(START), "until": _iso(END)}
    with pytest.raises(E2EError, match="truncated=True with next=None"):
        drill.read_run_logs(lambda _path: page, "/logs")


def test_repeated_lines_another_runs_lines_and_a_short_window_are_problems() -> None:
    lines = _lines(4)
    lines.append(dict(lines[0]))
    lines.append({**lines[1], "run_id": "another", "timestamp": _iso(END + 100)})
    read = drill.LogRead(since=_iso(START + 60), until=_iso(END - 60), pages=2, lines=lines)
    problems = drill.log_problems(read, _run())
    assert "1 line(s) repeated across pages" in problems
    assert "1 line(s) of another run, though only the run's were asked for" in problems
    assert any("after the run started" in p for p in problems)
    assert any("before the run ended" in p for p in problems)
    assert "the lines are not oldest first across pages" in problems


def test_the_evidence_line_holds_counts_and_no_text(api: FakeApi) -> None:
    read = drill.LogRead(since=_iso(START), until=_iso(END), pages=3, lines=_lines())
    line = drill.evidence(STAGE, api.failure, api.quality, 20, read, PODS, "analyst")
    prefix, _, body = line.partition(" ")
    record = json.loads(body)
    assert prefix == "RUN_DRILLDOWN_JSON"
    assert record["log_lines"] == 450 and record["leveled_lines"] == 45 and record["counts"]["warn"] == 1
    assert "line 1" not in line and "ValueError" not in line and REPORT_HREF not in line


class FakeDagster:
    def __init__(self) -> None:
        self.launched: dict[str, Any] = {}
        self.assets: dict[str, list[list[str]]] = {
            product.job_name: [["crm", "customers"], [product.domain_name, "orders"], [product.id, "daily_revenue"]]
            for product in E2E_INVENTORY.products
        }

    def discover_repository(self, job: str) -> tuple[str, str]:
        return "location", "repository"

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        paths = self.assets[(variables or {})["selector"]["pipelineName"]]
        return {"assetNodes": [{"assetKey": {"path": p}} for p in paths]}

    def launch(self, job: str, **kwargs: Any) -> str:
        self.launched = {"job": job, **kwargs}
        return RUN_ID

    def get_run(self, run_id: str) -> DagsterRun:
        return _run()


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch, api: FakeApi) -> tuple[FakeDagster, list[str], list[str]]:
    dagster, kubectl_calls, logged = FakeDagster(), [], []
    pods = {"left": [f"pod/{p}" for p in PODS]}

    @contextlib.contextmanager
    def session(*_args: Any, **_kwargs: Any) -> Iterator[ApiSession]:
        yield api.session()

    @contextlib.contextmanager
    def client(_cfg: Any) -> Iterator[FakeDagster]:
        yield dagster

    def kubectl(_cfg: Any, args: list[str], **_kwargs: Any) -> str:
        kubectl_calls.append(" ".join(args))
        if args[:2] == ["get", "pods"]:
            return "\n".join(pods["left"])
        if args[:2] == ["delete", "job"]:
            pods["left"] = []
        return ""

    monkeypatch.setattr(drill.metrics, "api_session", session)
    monkeypatch.setattr(drill, "dagster_client", client)
    monkeypatch.setattr(drill, "kubectl", kubectl)
    monkeypatch.setattr(drill.log, "info", logged.append)
    monkeypatch.setattr(drill.log, "step", logged.append)
    monkeypatch.setattr(drill.time, "sleep", lambda _s: None)
    return dagster, kubectl_calls, logged


def test_the_drill_fails_a_run_after_floe_deletes_its_jobs_and_reads_it_back(
    tmp_path: Path, api: FakeApi, deployed: tuple[FakeDagster, list[str], list[str]]
) -> None:
    dagster, kubectl_calls, logged = deployed
    drill.check_failed_run_drilldown(e2e_cfg(tmp_path))

    assert dagster.launched["job"] == PRODUCT.job_name
    assert dagster.launched["asset_selection"] == [[PRODUCT.domain_name, "orders"], [PRODUCT.id, "daily_revenue"]]
    assert dagster.launched["tags"]["openlakeforge/full_refresh"] == drill.REFUSED_SCOPE
    assert dagster.launched["tags"]["floe/full_refresh"] == "true"
    assert any(c.startswith("delete job -n lakehouse -l dagster/run-id=") for c in kubectl_calls)
    refused = [path for role, path in api.calls if role == "analyst"]
    assert len(refused) == 4 and REPORT_HREF in refused
    evidence = next(m for m in logged if m.startswith("RUN_DRILLDOWN_JSON"))
    assert json.loads(evidence.partition(" ")[2])["log_pages"] == 3


def test_a_report_served_as_something_other_than_json_fails_the_drill(
    tmp_path: Path, api: FakeApi, deployed: tuple[FakeDagster, list[str], list[str]]
) -> None:
    api.report = (b"<html>sign in</html>", "text/html")
    with pytest.raises(E2EError, match=re.escape("answered 'text/html', not JSON")):
        drill.check_failed_run_drilldown(e2e_cfg(tmp_path))


def test_a_route_a_role_below_the_floor_can_read_fails_the_drill(
    tmp_path: Path, api: FakeApi, deployed: tuple[FakeDagster, list[str], list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = api.get

    def leaky(path: str, role: str) -> requests.Response:
        return real(path, "data-engineer" if "/logs" in path else role)

    monkeypatch.setattr(api, "get", leaky)
    with pytest.raises(E2EError, match="analyst at .*/logs.*expected HTTP 403, got 200"):
        drill.check_failed_run_drilldown(e2e_cfg(tmp_path))


def test_a_deleted_pod_whose_lines_never_arrive_fails_after_the_deadline(
    tmp_path: Path, api: FakeApi, deployed: tuple[FakeDagster, list[str], list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    api.lines = [line for line in api.lines if line["pod"] == PODS[0]]
    clock = iter(range(0, 100_000, 600))
    monkeypatch.setattr(drill.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(E2EError, match=re.escape(f"no line of the deleted pod(s) ['{PODS[1]}']")):
        drill.check_failed_run_drilldown(e2e_cfg(tmp_path))


def test_a_default_product_reading_only_other_marts_leaves_the_drill_to_the_next_product(
    tmp_path: Path, api: FakeApi, deployed: tuple[FakeDagster, list[str], list[str]]
) -> None:
    dagster, _, _ = deployed
    api.failure.update(job=SECOND.job_name, domain=SECOND.domain_name, product=SECOND.id)
    dagster.assets[PRODUCT.job_name] = [["crm", "customers"], ["other_product", "mart"], [PRODUCT.id, "daily_revenue"]]
    drill.check_failed_run_drilldown(e2e_cfg(tmp_path))

    assert dagster.launched["job"] == SECOND.job_name
    assert [SECOND.domain_name, "orders"] in dagster.launched["asset_selection"]


def test_with_no_product_selecting_its_own_silver_the_drill_skips(
    tmp_path: Path, deployed: tuple[FakeDagster, list[str], list[str]]
) -> None:
    dagster, kubectl_calls, logged = deployed
    for product in E2E_INVENTORY.products:
        dagster.assets[product.job_name] = [["crm", "customers"], [product.id, "daily_revenue"]]
    drill.check_failed_run_drilldown(e2e_cfg(tmp_path))

    assert dagster.launched == {} and kubectl_calls == []
    assert logged == [
        "Skipping the failed-run drill-down: no product's job selects a Silver table of its own domain, "
        "so no Floe Job would run."
    ]


def test_a_product_named_like_its_domain_with_only_gold_models_is_not_taken_for_silver(
    tmp_path: Path, api: FakeApi, deployed: tuple[FakeDagster, list[str], list[str]]
) -> None:
    # A product sharing its domain's name keys its Gold models `[domain, model]`, like the domain's Silver tables.
    dagster, _, _ = deployed
    api.failure.update(job=SECOND.job_name, domain=SECOND.domain_name, product=SECOND.id)
    dagster.assets[PRODUCT.job_name] = [["crm", "customers"], [PRODUCT.domain_name, "mart_x"]]
    drill.check_failed_run_drilldown(e2e_cfg(tmp_path))

    assert dagster.launched["job"] == SECOND.job_name
