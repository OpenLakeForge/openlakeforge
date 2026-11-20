"""The operator journey through the deployed Portal (#794): overview, failed run, logs, quality, metrics.

A real Chromium, driven by Playwright, signs in through the perimeter as the browser checks do: Dex's GitHub connector
against the e2e stub, whose authorize page the browser cannot reach, so the context answers that one redirect as the
stub would (`authorize_redirect`, as `_identity_github.StubAuthorize` does for requests). The role is the lowest one
the logs row admits on this stage, so the run's logs and reports are on the page rather than refused.

The failed run is the one `_run_drilldown` left as the default product's latest run. Each page must reach the
state its screenshot is meant to show (a heading, a region, the run's own error text) before it is saved, and must
hold no Secret value; a page that does not is saved as `<name>.FAILED.png` beside the others and fails the check.

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
from olf.infra.e2e._run_drilldown import DRILL_TAG, REFUSED_SCOPE
from olf.infra.e2e._secret_exposure import find_exposures, secret_values
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, load_provider_contracts_or_raise

SCREENSHOT_DIR = ".tmp/walkthrough-screenshots"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
SCALE = 2
STATE_TIMEOUT_MS = 30_000
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


def resolver_rules(urls: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """Chromium flags pinning each host the way `olf.infra.access` pins it for requests: `*.localhost` to loopback,
    and a `tls: external` base domain to its `OLF_EDGE_RESOLVE` address."""
    rules = []
    for url in urls:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        address = "127.0.0.1" if host.endswith(".localhost") else pinned_address(host, environ)
        if address:
            rules.append(f"MAP {host} {address}")
    return [f"--host-resolver-rules={','.join(dict.fromkeys(rules))}"] if rules else []


def journey_failed_run(runs: Sequence[DagsterRun]) -> DagsterRun:
    """The drill-down's failed run, which must be the job's latest: the overview shows only the latest run's state."""
    if not runs:
        raise E2EError("the default product's job has no run; the failed-run drill-down runs before this check")
    latest = runs[0]
    if latest.status != "FAILURE" or DRILL_TAG not in latest.tags:
        raise E2EError(
            f"the default product's latest run {latest.run_id} is {latest.status}, not the drill-down's failed run; "
            "the failed-run drill-down runs right before this check"
        )
    return latest


def screenshot_names(metrics_served: bool) -> list[str]:
    return [
        "01-monitoring-overview",
        "02-failed-run",
        "03-run-logs",
        "04-run-quality",
        "05-metrics" if metrics_served else "05-metrics-off",
        "06-failed-run-390px",
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
            return f"the secret scan of the page could not complete ({type(exc).__name__})"
        return "; ".join(exposures) or None

    def shot(self, page: Any, name: str, reach: Callable[[], Any]) -> None:
        """`reach` drives `page` and asserts its state; it returns the element to capture, or None for the page."""
        try:
            target = reach()
        except Exception as exc:
            # What the page showed instead helps the reader, once it is shown to hold no Secret value; a capture that
            # fails must not replace the reason.
            if (unsafe := self.exposure(page, name)) is None:
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(self.directory / f"{name}.FAILED.png"), full_page=True)
                raise E2EError(
                    f"walkthrough {name}: the page did not reach its expected state (kept as FAILED): {exc}"
                ) from exc
            # Playwright's assertion text quotes what the page showed, which may be the value itself: only its type.
            raise E2EError(
                f"walkthrough {name}: the page did not reach its expected state ({type(exc).__name__}); "
                f"no screenshot saved: {unsafe}"
            ) from None
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
) -> None:
    """Overview, the failed run, its logs and quality, the Metrics tab, then the failed run at phone width."""
    expect = pw.expect
    page = context.new_page()
    page.set_default_timeout(STATE_TIMEOUT_MS)
    run_url = f"{portal}/monitoring/{stage}/runs/{run.run_id}"

    def region(name: str) -> Any:
        return page.get_by_role("region", name=name, exact=True)

    def overview() -> None:
        page.goto(f"{portal}/monitoring?stage={stage}")
        expect(page.get_by_role("heading", level=1, name="Monitoring")).to_be_visible()
        expect(
            region("What needs attention").get_by_role("link", name=f"Open run {_short(run.run_id)}")
        ).to_be_visible()

    def failed_run() -> None:
        region("What needs attention").get_by_role("link", name=f"Open run {_short(run.run_id)}").first.click()
        page.wait_for_url(re.compile(rf"/monitoring/{re.escape(stage)}/runs/{re.escape(run.run_id)}$"))
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        expect(region("What failed")).to_contain_text(REFUSED_SCOPE)

    def logs() -> Any:
        card = region("Logs")
        card.scroll_into_view_if_needed()
        card.get_by_role("checkbox", name="Match this run only").check()
        expect(region("Run log lines").get_by_role("row").first).to_be_visible()
        return card

    def quality() -> Any:
        card = region("Quality checks")
        card.scroll_into_view_if_needed()
        expect(region("Quality check results").get_by_role("row").nth(1)).to_be_visible()
        report = page.get_by_role("list", name="Quality reports").get_by_role("link").first
        prefix = re.escape(f"/api/v1/stages/{stage}/runs/{run.run_id}/quality/reports/")
        expect(report).to_have_attribute("href", re.compile(rf"^{prefix}[0-9a-f]{{16}}$"))
        return card

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

    overview_name, run_name, logs_name, quality_name, metrics_name, phone_name = names
    shooter.shot(page, overview_name, overview)
    shooter.shot(page, run_name, failed_run)
    shooter.shot(page, logs_name, logs)
    shooter.shot(page, quality_name, quality)
    shooter.shot(page, metrics_name, metrics_tab)
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

        shooter.shot(small, phone_name, failed_run_small)
    finally:
        phone.close()


@contextlib.contextmanager
def _signed_in_context(pw: Any, cfg: E2EConfig, urls: Sequence[str], login: str, ignore_https: bool) -> Iterator[Any]:
    """A desktop context whose browser answers the stub's authorize redirect as `login`."""
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(args=resolver_rules(urls, cfg_environ(cfg)))
        context = browser.new_context(viewport=DESKTOP, device_scale_factor=SCALE, ignore_https_errors=ignore_https)
        context.route(
            f"https://{stub_host(cfg)}/**",
            lambda r: r.fulfill(status=302, headers={"Location": authorize_redirect(r.request.url, login)}),
        )
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
    product = cfg.inventory.default_product
    with dagster_client(cfg) as client:
        run = journey_failed_run(client.list_runs(job_name=product.job_name, limit=1))
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
            _signed_in_context(pw, cfg, [portal, identity["issuer_url"]], login, ignore_https) as context,
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
            )
    log.info(f"Saved {len(shooter.saved)} walkthrough screenshots to {directory}.")
