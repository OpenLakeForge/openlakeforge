"""The operator journey through the deployed Portal (#794): overview, failed run, logs, quality, runs, metrics.

A real Chromium, driven by Playwright, signs in through the perimeter as the browser checks do: Dex's GitHub connector
against the e2e stub, whose authorize page the browser cannot reach. Playwright's `route` cannot answer that page: it
sees only the first request of a navigation, and the stub is the third redirect of one (portal, Dex, Dex's connector).
So the stub's name is made unresolvable in Chromium, the hop fails, and `sign_in` sends the browser where the stub
would have (`authorize_redirect`, as `_identity_github.StubAuthorize` does for requests). The role is the lowest one
the logs row admits on this stage, so the run's logs and reports are on the page rather than refused.

The failed run is the one `_run_drilldown` left as the latest run of its product, found the same way
(`drill_target`), so the walk skips where the drill-down did. Each page must reach the
state its screenshot is meant to show (a heading, a region, the run's own error text), with no card or chart still
showing its loading placeholder, before it is saved, and must hold no Secret value; a page that does not is saved
as `<name>.FAILED.png` beside the others and fails the check.

Playwright is not a dependency of `olf`: a `--checks all` run needs it installed beside `olf` with its Chromium
(the nightly does both), and this check fails without it rather than leaving the walkthrough out.
"""

from __future__ import annotations

import contextlib
import importlib
import re
import secrets
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from openlakeforge_services.dagster import DagsterRun

from olf.common import config, log
from olf.common.edge import pinned_address
from olf.infra import access
from olf.infra.e2e import _metrics as metrics
from olf.infra.e2e._dagster import dagster_client
from olf.infra.e2e._identity_github import dex_points_at_stub, github_stub, log_skip, stub_host
from olf.infra.e2e._perimeter import PORTAL_REF
from olf.infra.e2e._perimeter_bearer import LOGS_FLOOR, logs_admits
from olf.infra.e2e._run_drilldown import DRILL_TAG, REFUSED_SCOPE, drill_target, log_no_target
from olf.infra.e2e._secret_exposure import find_exposures, redact, secret_values
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, load_provider_contracts_or_raise

SCREENSHOT_DIR = ".tmp/walkthrough-screenshots"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
SCALE = 2
STATE_TIMEOUT_MS = 30_000
CHROMIUM_ERROR_PAGE = "chrome-error://"
# Portal 0.7.3 shows a card or chart that is still loading as a `status` reading "Reading …" (the run page's Logs
# card: "Reading the run's archived lines…"). Its other statuses are not placeholders and do not start that way.
LOADING_PLACEHOLDER = re.compile(r"^Reading .+…$")
INSTALL_HINT = (
    "install it beside olf with its browser, e.g. `uv tool install openlakeforge --with playwright` and "
    "`<that tool's python> -m playwright install --with-deps chromium`"
)


def authorize_redirect(url: str, login: str) -> str:
    """Where the stub's authorize page sends the browser: back to Dex with `code=<login>` and Dex's state."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    try:
        back, state = query["redirect_uri"][0], query["state"][0]
    except KeyError as exc:
        raise E2EError(f"the authorize redirect to the stub lacks {exc}") from exc
    return f"{back}?{urllib.parse.urlencode({'code': login, 'state': state})}"


def resolver_rules(urls: Sequence[str], environ: Mapping[str, str], unresolved: Sequence[str] = ()) -> list[str]:
    """Chromium flags pinning each host the way `olf.infra.access` pins it for requests: `*.localhost` to loopback,
    and a `tls: external` base domain to its `OLF_EDGE_RESOLVE` address. Each `unresolved` host fails to resolve,
    whatever the runner's DNS says, so a navigation to it fails at once."""
    rules = []
    for url in urls:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        address = "127.0.0.1" if host.endswith(".localhost") else pinned_address(host, environ)
        if address:
            rules.append(f"MAP {host} {address}")
    rules += [f"MAP {host} ~NOTFOUND" for host in unresolved]
    return [f"--host-resolver-rules={','.join(dict.fromkeys(rules))}"] if rules else []


def sign_in(page: Any, url: str, login: str, stub: str, navigation_error: type[Exception]) -> None:
    """Open `url`, through the perimeter's sign-in if it sends the browser to the `stub` host's authorize page.

    The hop to the stub fails (its name resolves only in the cluster); the browser then goes where the stub would have
    sent it, back to Dex as `login`, and on to `url`. A navigation that fails anywhere else fails here.
    """
    authorize: list[str] = []

    def note(request: Any) -> None:
        if urllib.parse.urlsplit(request.url).hostname == stub:
            authorize.append(request.url)

    page.on("request", note)
    try:
        page.goto(url)
    except navigation_error:
        if not authorize:
            raise
    finally:
        page.remove_listener("request", note)
    if authorize:
        # Chromium commits its error page after `goto` has raised; a navigation started before that is interrupted.
        page.wait_for_url(lambda at: at.startswith(CHROMIUM_ERROR_PAGE))
        page.goto(authorize_redirect(authorize[-1], login))


def journey_failed_run(job: str, runs: Sequence[DagsterRun]) -> DagsterRun:
    """The drill-down's failed run, which must be the job's latest: the overview shows only the latest run's state."""
    if not runs:
        raise E2EError(f"{job} has no run; the failed-run drill-down runs before this check")
    latest = runs[0]
    if latest.status != "FAILURE" or DRILL_TAG not in latest.tags:
        raise E2EError(
            f"{job}'s latest run {latest.run_id} is {latest.status}, not the drill-down's failed run; "
            "the failed-run drill-down runs right before this check"
        )
    return latest


def screenshot_names(metrics_served: bool) -> list[str]:
    return [
        "01-monitoring-overview",
        "02-failed-run",
        "03-run-logs",
        "04-run-quality",
        "05-runs",
        "06-logs-run",
        "07-metrics" if metrics_served else "07-metrics-off",
        "08-failed-run-390px",
    ]


class Shooter:
    """Saves a page's PNG only once it reached its state and holds no Secret value."""

    def __init__(self, directory: Path, secrets_by_label: Mapping[str, str]) -> None:
        self.directory = directory
        self.secrets = secrets_by_label
        self.saved: list[Path] = []

    def exposure(self, page: Any, name: str) -> str | None:
        """Why `page` must not be captured: a Secret value in its URL or HTML, or a scan that could not complete.

        The answer names Secret labels only, never a value. The screenshots are uploaded as an artifact, so a page
        that cannot be shown clean is not captured at all.
        """
        try:
            exposures = find_exposures(self.secrets, {f"the {name} page": f"{page.url}\n{page.content()}"})
        except Exception as exc:
            return f"the secret scan of the page could not complete ({self.describe(exc)})"
        return "; ".join(exposures) or None

    def describe(self, exc: BaseException) -> str:
        """The error's type and message with every Secret value redacted: Playwright's text quotes the page."""
        return f"{type(exc).__name__}: {redact(str(exc), self.secrets)}"

    def where(self, page: Any) -> str:
        try:
            return redact(str(page.url), self.secrets)
        except Exception as exc:
            return f"an unknown URL ({type(exc).__name__})"

    def shot(self, page: Any, name: str, reach: Callable[[], Any]) -> None:
        """`reach` drives `page` and asserts its state; it returns the element to capture, or None for the page."""
        try:
            target = reach()
        except Exception as exc:
            # What the page showed instead helps the reader, once it is shown to hold no Secret value; a capture that
            # fails must not replace the reason. Playwright's text quotes what the page showed, which may hold a
            # value, so it is reported redacted and the original is not chained.
            missed = f"walkthrough {name}: the page at {self.where(page)} did not reach its expected state"
            if (unsafe := self.exposure(page, name)) is None:
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(self.directory / f"{name}.FAILED.png"), full_page=True)
                raise E2EError(f"{missed} (kept as FAILED): {self.describe(exc)}") from None
            raise E2EError(f"{missed} ({self.describe(exc)}); no screenshot saved: {unsafe}") from None
        if unsafe := self.exposure(page, name):
            raise E2EError(f"walkthrough {name}: {unsafe}; no screenshot saved")
        path = self.directory / f"{name}.png"
        if target is None:
            page.screenshot(path=str(path), full_page=True)
        else:
            target.screenshot(path=str(path))
        self.saved.append(path)
        log.info(f"  {path.name}")


def _playwright() -> Any:
    try:
        return importlib.import_module("playwright.sync_api")
    except ImportError as exc:
        raise E2EError(f"the operator journey screenshots need Playwright: {INSTALL_HINT}") from exc


def _short(run_id: str) -> str:
    return run_id[:8]


def walk(
    pw: Any,
    context: Any,
    shooter: Shooter,
    *,
    portal: str,
    stage: str,
    run: DagsterRun,
    metrics_served: bool,
    names: Sequence[str],
    ignore_https: bool,
    login: str,
    stub: str,
) -> None:
    """Overview, the failed run, its logs and quality, its job's runs, the run on the Logs page, the Metrics tab, then
    the failed run at phone width. The selectors are the Portal's own (0.7.3), as its e2e tests use them."""
    expect = pw.expect
    page = context.new_page()
    page.set_default_timeout(STATE_TIMEOUT_MS)
    run_url = f"{portal}/monitoring/{stage}/runs/{run.run_id}"
    label = stage.upper()

    def region(name: str) -> Any:
        return page.get_by_role("region", name=name, exact=True)

    def loaded(on: Any, reach: Callable[[], Any]) -> Callable[[], Any]:
        """`reach`, then wait until no part of the page still shows its loading placeholder: the shot shows data."""

        def reached() -> Any:
            target = reach()
            # `expect` keeps its own 5 s default, not the page's: a run's log card waits on the archive read, which
            # the platform API bounds at 20 s, so the placeholder gets the state timeout.
            expect(on.get_by_role("status").filter(has_text=LOADING_PLACEHOLDER)).to_have_count(
                0, timeout=STATE_TIMEOUT_MS
            )
            return target

        return reached

    def overview() -> None:
        sign_in(page, f"{portal}/monitoring?stage={stage}", login, stub, pw.Error)
        expect(page.get_by_role("heading", level=1, name="Monitoring")).to_be_visible()
        # Times open in UTC until the reader picks Local, so every shot reads the same whatever the runner's zone.
        expect(page.get_by_role("group", name="Time zone").get_by_role("radio", name="UTC", exact=True)).to_be_checked()
        expect(
            region("What needs attention").get_by_role("link", name=f"Open run {_short(run.run_id)}")
        ).to_be_visible()

    def failed_run() -> None:
        region("What needs attention").get_by_role("link", name=f"Open run {_short(run.run_id)}").first.click()
        page.wait_for_url(re.compile(rf"/monitoring/{re.escape(stage)}/runs/{re.escape(run.run_id)}$"))
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        failed = region("What failed")
        expect(failed).to_contain_text(REFUSED_SCOPE)
        # The run's reports are linked from the failure summary, as same-origin downloads through the API.
        report = failed.get_by_role("list", name="Quality reports").get_by_role("link").first
        prefix = re.escape(f"/api/v1/stages/{stage}/runs/{run.run_id}/quality/reports/")
        expect(report).to_have_attribute("href", re.compile(rf"^{prefix}[0-9a-f]{{16}}$"))

    def logs() -> Any:
        card = region("Logs")
        card.scroll_into_view_if_needed()
        card.get_by_role("checkbox", name="Only this run's lines").check()
        # The card opens at warning and above; every level of the run's own lines is what the drill-down archived.
        card.get_by_role("combobox", name="Level").select_option(label="All levels")
        expect(region("Run log lines").get_by_role("row").nth(1)).to_be_visible()
        return card

    def quality() -> Any:
        card = region("Quality checks")
        card.scroll_into_view_if_needed()
        # Only the checks that did not pass are listed; the passes wait behind their count, opened for the shot.
        open_checks = region("Quality check results")
        passes = card.get_by_text(re.compile(r"^\d+ passed$"))
        expect(open_checks.or_(passes).first).to_be_visible()
        if passes.count():
            passes.click()
        expect(open_checks.or_(region("Passed quality checks")).first.get_by_role("row").nth(1)).to_be_visible()
        return card

    def runs() -> None:
        page.goto(f"{portal}/monitoring/{stage}/runs?job={urllib.parse.quote(run.job_name)}&status=failed")
        expect(
            region(f"{label} runs").get_by_role("row").get_by_role("link", name=f"Open run {_short(run.run_id)}")
        ).to_be_visible()

    def logs_run() -> None:
        page.goto(f"{portal}/monitoring/{stage}/logs?run={run.run_id}&match_run=true")
        expect(page.get_by_role("checkbox", name="Only this run's lines")).to_be_checked()
        expect(region(f"{label} log lines").get_by_role("row").nth(1)).to_be_visible()

    def metrics_tab() -> None:
        page.goto(f"{portal}/monitoring?stage={stage}")
        page.get_by_role("navigation", name="Monitoring views").get_by_role("link", name="Metrics").click()
        page.wait_for_url(re.compile(r"/monitoring/metrics\?"))
        if metrics_served:
            expect(region("CPU").get_by_role("group", name=re.compile(r"^CPU, last"))).to_be_visible()
        else:
            expect(
                page.get_by_role("heading", level=2, name=re.compile("Resource history is not collected"))
            ).to_be_visible()

    *steps, phone_name = names
    for name, reach in zip(steps, (overview, failed_run, logs, quality, runs, logs_run, metrics_tab), strict=True):
        shooter.shot(page, name, loaded(page, reach))
    page.close()

    phone = context.browser.new_context(
        viewport=PHONE,
        device_scale_factor=SCALE,
        ignore_https_errors=ignore_https,
        storage_state=context.storage_state(),
    )
    try:
        small = phone.new_page()
        small.set_default_timeout(STATE_TIMEOUT_MS)

        def failed_run_small() -> None:
            small.goto(run_url)
            expect(small.get_by_role("region", name="What failed", exact=True)).to_contain_text(REFUSED_SCOPE)

        shooter.shot(small, phone_name, loaded(small, failed_run_small))
    finally:
        phone.close()


@contextlib.contextmanager
def _desktop_context(pw: Any, cfg: E2EConfig, urls: Sequence[str], ignore_https: bool) -> Iterator[Any]:
    """A desktop context resolving `urls` as requests do, and failing at once on the stub's authorize page."""
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(args=resolver_rules(urls, cfg_environ(cfg), unresolved=[stub_host(cfg)]))
        context = browser.new_context(viewport=DESKTOP, device_scale_factor=SCALE, ignore_https_errors=ignore_https)
        try:
            yield context
        finally:
            browser.close()


def check_operator_journey_screenshots(cfg: E2EConfig) -> None:
    """Screenshots of the operator journey, each taken only once its page reached the state it is meant to show."""
    stage = cfg.namespace.removeprefix("olf-")
    contracts = load_provider_contracts_or_raise(cfg)
    access_contract, identity = contracts["shared"]["access"], contracts["shared"]["identity"]
    routes = {ref: route["url"] for ref, route in access_contract.get("routes", {}).items()}
    reasons = {
        "the contract declares no enabled perimeter": not (access_contract.get("perimeter") or {}).get("enabled"),
        "they need the Dex adapter": identity.get("adapter") != "dex",
        "the contract has no portal route": PORTAL_REF not in routes,
        "OLF_PORT_FORWARD bypasses the ingress": config.truthy(cfg_environ(cfg).get(access.PORT_FORWARD_ENV, "")),
    }
    if reason := next((text for text, applies in reasons.items() if applies), None):
        log.info(f"Skipping the operator journey screenshots: {reason}.")
        return
    pw = _playwright()
    with dagster_client(cfg) as client:
        if (target := drill_target(cfg, client)) is None:
            log_no_target("the operator journey screenshots")
            return
        job = target.product.job_name
        run = journey_failed_run(job, client.list_runs(job_name=job, limit=1))
    metrics_served = metrics.metrics_on(metrics.metrics_contract(cfg))
    directory = cfg.repo_root / SCREENSHOT_DIR / cfg.namespace
    directory.mkdir(parents=True, exist_ok=True)
    shooter = Shooter(directory, secret_values(cfg))
    portal = routes[PORTAL_REF].rstrip("/")
    with access.service_route(
        "shared/identity",
        service="dex",
        remote_port=5556,
        namespace=cfg.platform_namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-dex-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg_environ(cfg),
    ) as route:
        if not dex_points_at_stub(cfg):
            log_skip()
            return
        precedence, mapping = identity["roles"]["precedence"], identity["role_mapping"]
        role, _ = metrics.session_roles(precedence, mapping, LOGS_FLOOR, logs_admits(precedence, stage))
        if role is None:
            raise E2EError(f"the profile maps no team to a role that may read stage {stage}'s logs")
        login = f"olf-e2e-journey-{role}-{secrets.token_hex(3)}"
        log.step(f"Walking the operator journey through the Portal as {role} to failed run {run.run_id}...")
        # Chromium does not read the CA bundle requests are given: where the route needs the local CA, the browser
        # skips the certificate check instead.
        ignore_https = isinstance(route.verify, str)
        with (
            github_stub(cfg, {login: mapping[role]}),
            _desktop_context(pw, cfg, [portal, identity["issuer_url"]], ignore_https) as context,
        ):
            walk(
                pw,
                context,
                shooter,
                portal=portal,
                stage=stage,
                run=run,
                metrics_served=metrics_served,
                names=screenshot_names(metrics_served),
                ignore_https=ignore_https,
                login=login,
                stub=stub_host(cfg),
            )
    log.info(f"Saved {len(shooter.saved)} walkthrough screenshots to {directory}.")
