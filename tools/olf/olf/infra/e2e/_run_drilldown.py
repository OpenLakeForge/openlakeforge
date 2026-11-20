"""A failed product run read back through the platform API after its pods are gone (#794, scenario 2).

The drill makes the run itself. It launches the default product's job over its Silver and Gold assets with Floe's
full refresh, so the Floe Job reads every landed Bronze file and records its checks and reports, and with an
`openlakeforge/full_refresh` scope no runtime accepts, which the dbt step refuses before it builds anything. The run
fails after Floe, its failure hook publishes the quality manifest, and the failure is a plain one-line `ValueError`.
Bronze is left out of the selection: the Bronze step reads the same tag first and would fail the run before Floe.

Once the run has ended its Jobs are deleted, which is what their `ttlSecondsAfterFinished` does an hour later, so
every read below comes from the run's durable evidence: Dagster's event log, the quality manifest, the retained
reports and the log archive. The run is left failed for the walkthrough that follows (`_walkthrough`).

Every read goes through the portal host as a role the logs row admits on this stage, and a role below the routes'
floor must be refused on each. The evidence line carries counts only.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from openlakeforge_core import FLOE_FULL_REFRESH_TAG, FULL_REFRESH_TAG, Product
from openlakeforge_services.dagster import DagsterClient, DagsterRun

from olf.common import log
from olf.infra.e2e import _metrics as metrics
from olf.infra.e2e._dagster import dagster_client
from olf.infra.e2e._log_collector import ARCHIVE_POLL_SECONDS, ARCHIVE_TIMEOUT_SECONDS, RUN_ID_LABEL
from olf.infra.e2e._perimeter_bearer import LOGS_FLOOR, logs_admits
from olf.infra.e2e._quality_evidence import _JOB_ASSETS
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl

# No runtime accepts this scope (openlakeforge_core.FULL_REFRESH_SCOPES), so the first step that reads it fails.
REFUSED_SCOPE = "olf-e2e-refused-scope"
DRILL_TAG = "olf-e2e/run-drilldown"
RUN_TIMEOUT_SECONDS = 1800
JOB_DELETE_TIMEOUT_SECONDS = 300
# Small enough that a run's lines take several pages, so the cursor is exercised.
LOG_PAGE_LINES = 200
MAX_LOG_PAGES = 500
TERMINAL = {"SUCCESS", "FAILURE", "CANCELED"}
EVIDENCE_PREFIX = "RUN_DRILLDOWN_JSON"
# One second either side: Dagster reports float seconds, the API answers ISO strings.
WINDOW_SLACK_SECONDS = 1.0


def run_path(stage: str, run_id: str, suffix: str = "") -> str:
    return f"/api/v1/stages/{stage}/runs/{urllib.parse.quote(run_id, safe='')}{suffix}"


def drilldown_selection(asset_keys: Iterable[Sequence[str]], sources: Iterable[str]) -> list[list[str]]:
    """The job's asset keys without its Bronze ones: a Bronze key starts with its source's name."""
    bronze = set(sources)
    return [list(key) for key in asset_keys if key and key[0] not in bronze]


def failure_problems(failure: Mapping[str, Any], product: Product) -> list[str]:
    problems = []
    if failure.get("status") != "FAILURE":
        problems.append(f"status is {failure.get('status')!r}, not FAILURE")
    if (failure.get("domain"), failure.get("product")) != (product.domain_name, product.id):
        problems.append(
            f"scope is {failure.get('domain')!r}/{failure.get('product')!r}, "
            f"expected {product.domain_name!r}/{product.id!r}"
        )
    if not failure.get("steps"):
        problems.append("names no failed step")
    sources = {source.get("name"): source.get("status") for source in failure.get("sources") or []}
    if sources.get("dagster") != "available":
        problems.append(f"its dagster source is {sources.get('dagster')!r}")
    return problems


OUTCOMES = ("passed", "warn", "error", "not_run", "unknown")


def quality_problems(quality: Mapping[str, Any]) -> list[str]:
    """The run's checks are there, and the counts are their outcomes: a warn is never counted as an error."""
    checks = quality.get("checks")
    counts = quality.get("counts")
    if not checks:
        return [f"no checks: {quality.get('sources')}"]
    if not isinstance(counts, Mapping):
        return ["checks but no counts"]
    tallies = {outcome: sum(1 for check in checks if check.get("outcome") == outcome) for outcome in OUTCOMES}
    problems = [
        f"{outcome} is counted {counts.get(outcome)}, the checks hold {tallies[outcome]}"
        for outcome in OUTCOMES
        if counts.get(outcome) != tallies[outcome]
    ]
    if not any(check.get("engine") == "floe" for check in checks):
        problems.append("no Floe check, though the Floe step ran")
    return problems


def report_href_problems(href: str | None, stage: str, run_id: str) -> list[str]:
    """A report is served by the API on its own origin, never as an object-store link."""
    if not href:
        return ["no report has an href"]
    parts = urllib.parse.urlsplit(href)
    problems = []
    if parts.scheme or parts.netloc:
        problems.append(f"href {href!r} names an origin; it must be a same-origin path")
    if not parts.path.startswith(run_path(stage, run_id, "/quality/reports/")):
        problems.append(f"href {parts.path!r} is not under the run's report route")
    if parts.query:
        problems.append("href carries a query string, as a presigned object-store URL would")
    return problems


def parse_time(value: str) -> datetime:
    """An ISO 8601 time with any number of fractional digits (the archive keeps nanoseconds), as UTC."""
    text = value.strip().replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        digits = "".join(c for c in rest if c.isdigit())
        text = f"{head}.{digits[:6]}{rest[len(digits) :]}"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class LogRead:
    """Every page of one run's log window, as the API answered them."""

    since: str = ""
    until: str = ""
    pages: int = 0
    lines: list[dict[str, Any]] = field(default_factory=list)


def read_run_logs(get: Callable[[str], Mapping[str, Any]], path: str, *, max_pages: int = MAX_LOG_PAGES) -> LogRead:
    """Follow `next` from `path` until it is null. A cursor that comes back twice would loop, so it fails."""
    read = LogRead()
    after: str | None = None
    seen: set[str] = set()
    while True:
        separator = "&" if "?" in path else "?"
        page = get(path if after is None else f"{path}{separator}after={urllib.parse.quote(after, safe='')}")
        if read.pages == 0:
            read.since, read.until = str(page["since"]), str(page["until"])
        read.pages += 1
        read.lines.extend(page.get("lines") or [])
        following = page.get("next")
        if bool(page.get("truncated")) != (following is not None):
            raise E2EError(f"page {read.pages} says truncated={page.get('truncated')} with next={following!r}")
        if following is None:
            return read
        if following in seen:
            raise E2EError(f"page {read.pages} returned a cursor an earlier page returned")
        if read.pages >= max_pages:
            raise E2EError(f"the run's logs did not end within {max_pages} pages")
        seen.add(following)
        after = following


def _identity(line: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(line.get(key) for key in ("timestamp", "namespace", "pod", "container", "stream", "line"))


def log_problems(read: LogRead, run: DagsterRun) -> list[str]:
    """The pages cover the run's window, repeat no line, hold only the run's lines, and keep them oldest first."""
    problems = []
    start = run.started_at if run.started_at is not None else run.created_at
    since, until = parse_time(read.since).timestamp(), parse_time(read.until).timestamp()
    if since > start + WINDOW_SLACK_SECONDS:
        problems.append(f"the window starts at {read.since}, after the run started")
    if run.ended_at is not None and until < run.ended_at - WINDOW_SLACK_SECONDS:
        problems.append(f"the window ends at {read.until}, before the run ended")
    identities = [_identity(line) for line in read.lines]
    if repeated := len(identities) - len(set(identities)):
        problems.append(f"{repeated} line(s) repeated across pages")
    if others := sum(1 for line in read.lines if line.get("run_id") != run.run_id):
        problems.append(f"{others} line(s) of another run, though only the run's were asked for")
    times = []
    for line in read.lines:
        if not line.get("timestamp"):
            continue
        times.append(parse_time(str(line["timestamp"])).timestamp())
    if untimed := len(read.lines) - len(times):
        problems.append(f"{untimed} line(s) of a v2 record carry no timestamp")
    if any(later < earlier for earlier, later in zip(times, times[1:], strict=False)):
        problems.append("the lines are not oldest first across pages")
    if outside := sum(1 for t in times if not since - WINDOW_SLACK_SECONDS <= t <= until + WINDOW_SLACK_SECONDS):
        problems.append(f"{outside} line(s) outside the window")
    return problems


def missing_pods(read: LogRead, pods: Sequence[str]) -> list[str]:
    return sorted(set(pods) - {str(line.get("pod")) for line in read.lines})


def evidence(
    stage: str,
    failure: Mapping[str, Any],
    quality: Mapping[str, Any],
    report_bytes: int,
    read: LogRead,
    pods: Sequence[str],
    refused: str | None,
) -> str:
    """One line of counts: no message, row, value or URL."""
    record = {
        "stage": stage,
        "run_id": failure.get("run_id"),
        "job": failure.get("job"),
        "domain": failure.get("domain"),
        "product": failure.get("product"),
        "status": failure.get("status"),
        "failed_steps": len(failure.get("steps") or []),
        "checks": len(quality.get("checks") or []),
        "counts": dict(quality.get("counts") or {}),
        "manifest_complete": quality.get("complete"),
        "reports": len(quality.get("reports") or []),
        "report_bytes": report_bytes,
        "deleted_pods": len(pods),
        "log_pages": read.pages,
        "log_lines": len(read.lines),
        "leveled_lines": sum(1 for line in read.lines if line.get("level")),
        "refused_role": refused,
    }
    return f"{EVIDENCE_PREFIX} {json.dumps(record, sort_keys=True)}"


def _fail_a_product_run(cfg: E2EConfig, client: DagsterClient, product: Product) -> DagsterRun:
    location, repository = client.discover_repository(product.job_name)
    selector = {"pipelineName": product.job_name, "repositoryName": repository, "repositoryLocationName": location}
    nodes = client.graphql(_JOB_ASSETS, {"selector": selector}).get("assetNodes") or []
    selection = drilldown_selection(
        (node["assetKey"]["path"] for node in nodes), (source.name for source in cfg.inventory.sources)
    )
    if not any(key[0] == product.domain_name for key in selection):
        raise E2EError(f"{product.job_name} selects no {product.domain_name} Silver table; no Floe Job would run")
    tags = {FULL_REFRESH_TAG: REFUSED_SCOPE, FLOE_FULL_REFRESH_TAG: "true", DRILL_TAG: "failed-run"}
    run_id = client.launch(product.job_name, tags=tags, asset_selection=selection)
    log.info(f"{product.job_name}: launched {run_id} to fail at its dbt step, after Floe.")
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while (run := client.get_run(run_id)) is None or run.status not in TERMINAL:
        if time.monotonic() >= deadline:
            raise E2EError(f"run {run_id} did not end in {RUN_TIMEOUT_SECONDS}s")
        time.sleep(10)
    if run.status != "FAILURE":
        raise E2EError(f"run {run_id} was made to fail at its dbt step, but ended {run.status}")
    return run


def _delete_run_jobs(cfg: E2EConfig, run_id: str) -> list[str]:
    """Delete the run's Jobs as their TTL would, wait until no pod of the run is left, and name the pods."""
    selector = f"{RUN_ID_LABEL}={run_id}"
    names = kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-l", selector, "-o", "name"], capture=True).split()
    pods = [name.removeprefix("pod/") for name in names]
    if not pods:
        raise E2EError(f"run {run_id} has no pod labelled {selector} to delete")
    kubectl(cfg, ["delete", "job", "-n", cfg.namespace, "-l", selector, "--cascade=foreground", "--timeout=300s"])
    deadline = time.monotonic() + JOB_DELETE_TIMEOUT_SECONDS
    while kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-l", selector, "-o", "name"], capture=True).strip():
        if time.monotonic() >= deadline:
            raise E2EError(f"the pods of run {run_id} were not gone {JOB_DELETE_TIMEOUT_SECONDS}s after their Jobs")
        time.sleep(3)
    return pods


def _await_logs(api: metrics.ApiSession, stage: str, run: DagsterRun, pods: Sequence[str]) -> LogRead:
    """Read the run's window until every deleted pod's lines are archived; the collector uploads every 5 minutes."""
    path = run_path(stage, run.run_id, f"/logs?match_run=true&limit={LOG_PAGE_LINES}")
    deadline = time.monotonic() + ARCHIVE_TIMEOUT_SECONDS
    while True:
        read = read_run_logs(api.read, path)
        if problems := log_problems(read, run):
            raise E2EError(f"run {run.run_id}'s logs through the API: {'; '.join(problems)}")
        if not (missing := missing_pods(read, pods)):
            return read
        if time.monotonic() >= deadline:
            raise E2EError(f"run {run.run_id}'s logs hold no line of the deleted pod(s) {missing}")
        log.info(f"  {len(read.lines)} archived line(s) of run {run.run_id} so far; waiting for {len(missing)} pod(s).")
        time.sleep(ARCHIVE_POLL_SECONDS)


def _download_report(api: metrics.ApiSession, quality: Mapping[str, Any], stage: str, run_id: str) -> int:
    reports = [r for r in quality.get("reports") or [] if r.get("href")]
    href = next((r["href"] for r in reports if r.get("engine") == "floe"), reports[0]["href"] if reports else None)
    if problems := report_href_problems(href, stage, run_id):
        raise E2EError(f"run {run_id}'s report link: {'; '.join(problems)}")
    reply = api.get(str(href), api.reader)
    if reply.status_code != 200:
        raise E2EError(f"downloading {href}: HTTP {reply.status_code}, expected 200 from the API itself")
    if not reply.headers.get("Content-Type", "").startswith("application/json"):
        raise E2EError(f"{href} answered {reply.headers.get('Content-Type')!r}, not JSON")
    try:
        json.loads(reply.content)
    except ValueError as exc:
        raise E2EError(f"{href} is not a JSON document: {exc}") from exc
    return len(reply.content)


def _check_refused(api: metrics.ApiSession, stage: str, run_id: str, href: str) -> None:
    if api.refused is None:
        log.info(f"Not checking a refusal: the profile maps no team to a role below {LOGS_FLOOR}.")
        return
    for path in (
        run_path(stage, run_id, "/failure"),
        run_path(stage, run_id, "/quality"),
        href,
        run_path(stage, run_id, "/logs?match_run=true&limit=1"),
    ):
        reply = api.get(path, api.refused)
        if reply.status_code != 403:
            raise E2EError(f"{api.refused} at {path}: expected HTTP 403, got {reply.status_code}")


def check_failed_run_drilldown(cfg: E2EConfig) -> None:
    """A failed run whose pods are deleted: its failure, checks, a report and its whole log window, through the API."""
    stage = cfg.namespace.removeprefix("olf-")
    product = cfg.inventory.default_product
    with metrics.api_session(
        cfg,
        floor=LOGS_FLOOR,
        admits=lambda precedence: logs_admits(precedence, stage),
        what="the failed-run drill-down",
    ) as api:
        if api is None:
            return
        log.step(f"Failing a {product.job_name} run after Floe and reading it back through the API as {api.reader}...")
        with dagster_client(cfg) as client:
            run = _fail_a_product_run(cfg, client, product)
        pods = _delete_run_jobs(cfg, run.run_id)
        log.info(f"Deleted the Jobs of run {run.run_id}; its {len(pods)} pod(s) are gone.")
        failure = api.read(run_path(stage, run.run_id, "/failure"))
        if problems := failure_problems(failure, product):
            raise E2EError(f"run {run.run_id}'s failure: {'; '.join(problems)}")
        quality = api.read(run_path(stage, run.run_id, "/quality"))
        if problems := quality_problems(quality):
            raise E2EError(f"run {run.run_id}'s quality: {'; '.join(problems)}")
        report_bytes = _download_report(api, quality, stage, run.run_id)
        read = _await_logs(api, stage, run, pods)
        href = next(r["href"] for r in quality["reports"] if r.get("href"))
        _check_refused(api, stage, run.run_id, href)
        log.info(evidence(stage, failure, quality, report_bytes, read, pods, api.refused))
