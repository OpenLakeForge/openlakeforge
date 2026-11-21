from __future__ import annotations

import contextlib
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import E2E_INVENTORY, e2e_cfg
from openlakeforge_services.dagster import DagsterRun

from olf.infra.e2e import _walkthrough as walkthrough
from olf.infra.e2e._run_drilldown import DRILL_TAG
from olf.infra.e2e._shell import E2EError

RUN_ID = "0f6c3a52-8d3e-4c4f-9a65-2b7f1c9d0e11"
PORTAL = "https://portal.olf.localhost"


def _run(status: str = "FAILURE", tags: dict[str, str] | None = None) -> DagsterRun:
    return DagsterRun(
        RUN_ID, "order_revenue_pipeline", status, 1.0, 2.0, 3.0, {DRILL_TAG: "x"} if tags is None else tags
    )


def test_the_stub_redirect_goes_back_to_dex_with_the_login_as_code() -> None:
    url = (
        "https://olf-e2e-github.olf-system.svc/login/oauth/authorize?redirect_uri=https%3A%2F%2Fdex%2Fcallback&state=s1"
    )
    assert walkthrough.authorize_redirect(url, "olf-e2e-x") == "https://dex/callback?code=olf-e2e-x&state=s1"
    with pytest.raises(E2EError, match="lacks 'state'"):
        walkthrough.authorize_redirect("https://stub/authorize?redirect_uri=x", "olf-e2e-x")


def test_chromium_resolves_hosts_as_the_requests_routes_do() -> None:
    environ = {"OLF_EDGE_RESOLVE": "lake.example.com=10.1.2.3"}
    rules = walkthrough.resolver_rules(
        [PORTAL, "https://id.olf.localhost/dex", "https://portal.lake.example.com", "https://elsewhere.org"], environ
    )
    assert rules == [
        "--host-resolver-rules=MAP portal.olf.localhost 127.0.0.1,MAP id.olf.localhost 127.0.0.1,"
        "MAP portal.lake.example.com 10.1.2.3"
    ]
    assert walkthrough.resolver_rules(["https://elsewhere.org"], {}) == []
    assert walkthrough.resolver_rules([PORTAL], {}, unresolved=[STUB]) == [
        f"--host-resolver-rules=MAP portal.olf.localhost 127.0.0.1,MAP {STUB} ~NOTFOUND"
    ]


STUB = "olf-e2e-github.olf-system.svc"


class FakeNavigationError(Exception):
    pass


class SignInPage:
    """A page whose one navigation lands on `url`, or fails with `refusal` without leaving for the stub.

    How the hop to the stub fails, and what Chromium does after it, is a browser's to show:
    `test_e2e_walkthrough_browser.py` drives the real one.
    """

    def __init__(self, refusal: str | None = None) -> None:
        self.refusal, self.visited, self.listeners = refusal, [], []  # type: ignore[var-annotated]

    def on(self, _event: str, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_listener(self, _event: str, listener: Any) -> None:
        self.listeners.remove(listener)

    def goto(self, url: str) -> None:
        self.visited.append(url)
        if self.refusal:
            raise FakeNavigationError(self.refusal)


def test_sign_in_is_one_navigation_once_signed_in_and_any_other_failure_is_raised() -> None:
    overview = f"{PORTAL}/monitoring?stage=dev"
    page = SignInPage()
    walkthrough.sign_in(page, overview, "olf-e2e-x", STUB, FakeNavigationError)
    assert page.visited == [overview] and page.listeners == []

    refused = SignInPage("net::ERR_CERT_AUTHORITY_INVALID")
    with pytest.raises(FakeNavigationError, match="CERT"):
        walkthrough.sign_in(refused, overview, "olf-e2e-x", STUB, FakeNavigationError)
    assert refused.listeners == []


def test_the_journey_needs_the_drill_downs_failed_run_as_the_latest() -> None:
    job = "order_revenue_pipeline"
    assert walkthrough.journey_failed_run(job, [_run()]).run_id == RUN_ID
    with pytest.raises(E2EError, match="runs before this check"):
        walkthrough.journey_failed_run(job, [])
    with pytest.raises(E2EError, match="is SUCCESS"):
        walkthrough.journey_failed_run(job, [_run("SUCCESS")])
    with pytest.raises(E2EError, match="not the drill-down's failed run"):
        walkthrough.journey_failed_run(job, [_run(tags={})])


def test_the_metrics_shot_is_named_for_what_it_shows() -> None:
    assert walkthrough.screenshot_names(True)[6] == "07-metrics"
    assert walkthrough.screenshot_names(False)[6] == "07-metrics-off"
    assert len(walkthrough.screenshot_names(True)) == 8


class FakeLocator:
    def __init__(self, page: FakePage, path: tuple[str, ...]) -> None:
        self.page, self.path = page, path
        self.has_text: re.Pattern[str] | None = None

    def get_by_role(self, role: str, **kwargs: Any) -> FakeLocator:
        return FakeLocator(self.page, (*self.path, f"{role}:{kwargs.get('name', '')}"))

    def get_by_text(self, text: Any) -> FakeLocator:
        return FakeLocator(self.page, (*self.path, f"text:{getattr(text, 'pattern', text)}"))

    def filter(self, has_text: re.Pattern[str]) -> FakeLocator:
        located = FakeLocator(self.page, self.path)
        located.has_text = has_text
        return located

    def or_(self, other: FakeLocator) -> FakeLocator:
        return self if self.count() else other

    def count(self) -> int:
        return 0 if any(m in part for m in self.page.missing for part in self.path) else 1

    def select_option(self, label: str) -> None:
        FakeExpect(self).to_be_visible()

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, _index: int) -> FakeLocator:
        return self

    def click(self) -> None:
        if any("Open run" in part for part in self.path):
            self.page.url = f"{PORTAL}/monitoring/lakehouse/runs/{RUN_ID}"

    def check(self) -> None:
        FakeExpect(self).to_be_visible()  # an action waits for its element, as an assertion does

    def scroll_into_view_if_needed(self) -> None:
        pass

    def screenshot(self, path: str) -> None:
        self.page.saved.append(Path(path).name)


class FakePage(FakeLocator):
    """A page on which every locator is there, except those whose accessible names `missing` lists.

    `statuses` maps a URL fragment to the `status` texts a page at a URL holding it keeps showing.
    """

    def __init__(
        self,
        missing: set[str],
        shown: str = "",
        saved: list[str] | None = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        super().__init__(self, ())
        self.missing, self.shown, self.url = missing, shown, PORTAL
        self.statuses = statuses or {}
        self.saved = [] if saved is None else saved

    def goto(self, url: str) -> None:
        self.url = url

    def on(self, _event: str, _listener: Any) -> None:
        pass

    def remove_listener(self, _event: str, _listener: Any) -> None:
        pass

    def set_default_timeout(self, _ms: int) -> None:
        pass

    def wait_for_url(self, _pattern: Any) -> None:
        pass

    def content(self) -> str:
        return f"<html>{self.shown}</html>"

    def close(self) -> None:
        pass

    def screenshot(self, path: str, full_page: bool = False) -> None:
        assert full_page
        self.saved.append(Path(path).name)


class FakeExpect:
    def __init__(self, locator: FakeLocator) -> None:
        self.locator = locator

    def _there(self) -> None:
        if hits := [m for m in self.locator.page.missing if any(m in part for part in self.locator.path)]:
            raise AssertionError(f"{hits} not visible")

    def to_be_visible(self) -> None:
        self._there()

    def to_contain_text(self, _text: str) -> None:
        self._there()

    def to_have_attribute(self, _name: str, _value: Any) -> None:
        self._there()

    def to_be_checked(self) -> None:
        self._there()

    def to_have_count(self, count: int, timeout: float | None = None) -> None:
        assert timeout == walkthrough.STATE_TIMEOUT_MS, "the loading wait must outlast the archive read's 20 s deadline"
        page, pattern = self.locator.page, self.locator.has_text
        assert self.locator.path[-1] == "status:" and pattern is not None
        shown = [text for fragment, text in page.statuses.items() if fragment in page.url and pattern.search(text)]
        if len(shown) != count:
            raise AssertionError(f"{shown} still shown")


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.browser = self
        self.options: dict[str, Any] = {}

    def new_page(self) -> FakePage:
        return self.page

    def storage_state(self) -> dict[str, Any]:
        return {"cookies": []}

    def new_context(self, **options: Any) -> FakeContext:
        phone = FakeContext(FakePage(self.page.missing, saved=self.page.saved, statuses=self.page.statuses))
        phone.options = options
        self.options = options
        return phone

    def close(self) -> None:
        pass


class FakePlaywright:
    expect = FakeExpect
    Error = FakeNavigationError


def _walk(tmp_path: Path, page: FakePage, secrets: dict[str, str] | None = None) -> walkthrough.Shooter:
    shooter = walkthrough.Shooter(tmp_path, secrets or {"a client secret": "s3cr3t-value-0123456789"})
    walkthrough.walk(
        FakePlaywright(),
        FakeContext(page),
        shooter,
        portal=PORTAL,
        stage="lakehouse",
        run=_run(),
        metrics_served=False,
        names=walkthrough.screenshot_names(False),
        ignore_https=True,
        login="olf-e2e-x",
        stub=STUB,
    )
    return shooter


def test_every_page_that_reached_its_state_is_saved_in_order(tmp_path: Path) -> None:
    page = FakePage(set())
    context = FakeContext(page)
    shooter = walkthrough.Shooter(tmp_path, {"a client secret": "s3cr3t-value-0123456789"})
    walkthrough.walk(
        FakePlaywright(),
        context,
        shooter,
        portal=PORTAL,
        stage="lakehouse",
        run=_run(),
        metrics_served=False,
        names=walkthrough.screenshot_names(False),
        ignore_https=True,
        login="olf-e2e-x",
        stub=STUB,
    )
    assert page.saved == [f"{name}.png" for name in walkthrough.screenshot_names(False)]
    assert context.options["viewport"] == walkthrough.PHONE and context.options["device_scale_factor"] == 2


def test_a_page_that_missed_its_state_is_kept_as_failed_and_fails_the_walk(tmp_path: Path) -> None:
    page = FakePage({"Quality check results", "Passed quality checks"})
    with pytest.raises(
        E2EError, match=r"04-run-quality: the page at \S+ did not reach its expected state \(kept as FAILED\)"
    ):
        _walk(tmp_path, page)
    assert page.saved == [
        "01-monitoring-overview.png",
        "02-failed-run.png",
        "03-run-logs.png",
        "04-run-quality.FAILED.png",
    ]


SECRET = "s3cr3t-value-0123456789"


def _miss() -> None:
    raise AssertionError("heading not visible")


def test_a_failing_clean_page_is_kept_as_failed(tmp_path: Path) -> None:
    page = FakePage(set())
    with pytest.raises(E2EError, match=r"02-failed-run: .*\(kept as FAILED\)"):
        walkthrough.Shooter(tmp_path, {"a client secret": SECRET}).shot(page, "02-failed-run", _miss)
    assert page.saved == ["02-failed-run.FAILED.png"]


def test_a_failing_page_showing_a_secret_is_not_captured_and_the_value_is_not_reported(tmp_path: Path) -> None:
    page = FakePage(set(), shown=f"error: {SECRET}")
    with pytest.raises(E2EError) as raised:
        walkthrough.Shooter(tmp_path, {"a client secret": SECRET}).shot(page, "02-failed-run", _miss)
    assert page.saved == []
    assert "no screenshot saved" in str(raised.value) and "Secret a client secret" in str(raised.value)
    assert SECRET not in str(raised.value)


def test_a_failing_page_whose_scan_cannot_complete_is_not_captured(tmp_path: Path) -> None:
    page = FakePage(set())

    def unreadable() -> str:
        raise RuntimeError(f"target closed {SECRET}")

    page.content = unreadable  # type: ignore[method-assign]
    with pytest.raises(E2EError) as raised:
        walkthrough.Shooter(tmp_path, {"a client secret": SECRET}).shot(page, "02-failed-run", _miss)
    assert page.saved == []
    assert "secret scan of the page could not complete (RuntimeError: target closed <redacted:a client secret>)" in str(
        raised.value
    )
    assert SECRET not in str(raised.value)


def test_a_page_that_never_loaded_reports_where_it_stopped_and_why_with_secret_values_redacted(tmp_path: Path) -> None:
    """The first full run's failure: the navigation failed, so the scan could not read the page either."""
    page = FakePage(set())
    page.url = f"https://dex/callback?code={SECRET}"

    def navigating() -> str:
        raise FakeNavigationError("Unable to retrieve content because the page is navigating")

    def unreachable() -> None:
        raise FakeNavigationError(f"net::ERR_NAME_NOT_RESOLVED at {page.url}\nCall log:\n  - navigating to ...")

    page.content = navigating  # type: ignore[method-assign]
    with pytest.raises(E2EError) as raised:
        walkthrough.Shooter(tmp_path, {"a client secret": SECRET}).shot(page, "01-monitoring-overview", unreachable)
    message = str(raised.value)
    assert SECRET not in message and SECRET[:8] not in message
    assert message == (
        "walkthrough 01-monitoring-overview: the page at https://dex/callback?code=<redacted:a client secret> did not "
        "reach its expected state (FakeNavigationError: net::ERR_NAME_NOT_RESOLVED at "
        "https://dex/callback?code=<redacted:a client secret> Call log: - navigating to ...); no screenshot saved: "
        "the secret scan of the page could not complete (FakeNavigationError: Unable to retrieve content because the "
        "page is navigating)"
    )


def test_a_clean_page_reports_the_error_redacted_too(tmp_path: Path) -> None:
    page = FakePage(set())

    def quoting() -> None:
        raise AssertionError(f"expected text, received {SECRET[:12]}…")

    with pytest.raises(E2EError) as raised:
        walkthrough.Shooter(tmp_path, {"a client secret": SECRET}).shot(page, "02-failed-run", quoting)
    assert page.saved == ["02-failed-run.FAILED.png"]
    assert str(raised.value).endswith(
        "(kept as FAILED): AssertionError: expected text, received <redacted:a client secret>…"
    )


def test_a_page_showing_a_secret_value_is_not_saved(tmp_path: Path) -> None:
    page = FakePage(set(), shown="token s3cr3t-value-0123456789")
    with pytest.raises(E2EError, match="01-monitoring-overview: .*no screenshot saved"):
        _walk(tmp_path, page)
    assert page.saved == []


@pytest.mark.parametrize(
    ("label", "shot"),
    [
        ("What needs attention", "01-monitoring-overview"),
        ("Time zone", "01-monitoring-overview"),
        ("Quality reports", "02-failed-run"),
        ("Only this run's lines", "03-run-logs"),
        ("Run log lines", "03-run-logs"),
        ("LAKEHOUSE runs", "05-runs"),
        ("LAKEHOUSE log lines", "06-logs-run"),
    ],
)
def test_each_shot_waits_for_the_portal_label_it_shows(tmp_path: Path, label: str, shot: str) -> None:
    page = FakePage({label})
    with pytest.raises(E2EError, match=f"walkthrough {shot}: "):
        _walk(tmp_path, page)
    assert f"{shot}.FAILED.png" in page.saved


def test_a_run_whose_checks_all_passed_shows_them_opened(tmp_path: Path) -> None:
    page = FakePage({"Quality check results"})
    _walk(tmp_path, page)
    assert "04-run-quality.png" in page.saved


def test_the_failed_run_screen_must_show_the_runs_own_error(tmp_path: Path) -> None:
    page = FakePage({"What failed"})
    with pytest.raises(E2EError, match="02-failed-run"):
        _walk(tmp_path, page)
    assert "02-failed-run.png" not in page.saved


def test_without_playwright_the_check_fails_and_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(E2EError, match="need Playwright: install it beside olf"):
        walkthrough._playwright()


def test_a_deployment_without_a_perimeter_skips_the_walk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    messages: list[str] = []
    contracts = {"shared": {"access": {"routes": {}, "perimeter": {"enabled": False}}, "identity": {"adapter": "dex"}}}
    monkeypatch.setattr(walkthrough, "load_provider_contracts_or_raise", lambda _cfg: contracts)
    monkeypatch.setattr(walkthrough.log, "info", messages.append)
    monkeypatch.setattr(walkthrough, "_playwright", lambda: pytest.fail("no browser is needed to skip"))
    walkthrough.check_operator_journey_screenshots(e2e_cfg(tmp_path))
    assert messages == ["Skipping the operator journey screenshots: the contract declares no enabled perimeter."]


class FakeDagster:
    """Each job selects its domain's Silver unless `gold_only` names it; every job's latest run succeeded."""

    def __init__(self, gold_only: set[str]) -> None:
        self.gold_only = gold_only

    def discover_repository(self, job: str) -> tuple[str, str]:
        return "location", "repository"

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        job = (variables or {})["selector"]["pipelineName"]
        product = next(p for p in E2E_INVENTORY.products if p.job_name == job)
        paths = (
            [[product.id, "mart"]] if job in self.gold_only else [[product.domain_name, "orders"], [product.id, "mart"]]
        )
        return {"assetNodes": [{"assetKey": {"path": p}} for p in paths]}

    def list_runs(self, *, job_name: str = "", limit: int = 20) -> list[DagsterRun]:
        return [DagsterRun(RUN_ID, job_name, "SUCCESS", 1.0, 2.0, 3.0, {})]


@pytest.fixture
def perimeter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    contracts = {
        "shared": {
            "access": {"routes": {"shared/portal": {"url": PORTAL}}, "perimeter": {"enabled": True}},
            "identity": {"adapter": "dex"},
        }
    }
    monkeypatch.setattr(walkthrough, "load_provider_contracts_or_raise", lambda _cfg: contracts)
    monkeypatch.setattr(walkthrough.log, "info", messages.append)
    monkeypatch.setattr(walkthrough, "_playwright", lambda: object())
    monkeypatch.delenv(walkthrough.access.PORT_FORWARD_ENV, raising=False)
    return messages


def _dagster(monkeypatch: pytest.MonkeyPatch, gold_only: set[str]) -> None:
    @contextlib.contextmanager
    def client(_cfg: Any) -> Iterator[FakeDagster]:
        yield FakeDagster(gold_only)

    monkeypatch.setattr(walkthrough, "dagster_client", client)


def test_the_walk_looks_for_the_failed_run_of_the_product_the_drill_down_chose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, perimeter: list[str]
) -> None:
    default, second = E2E_INVENTORY.products[0], E2E_INVENTORY.products[1]
    _dagster(monkeypatch, {default.job_name})
    with pytest.raises(E2EError, match=f"^{second.job_name}'s latest run {RUN_ID} is SUCCESS"):
        walkthrough.check_operator_journey_screenshots(e2e_cfg(tmp_path))


def test_with_no_product_selecting_its_own_silver_the_walk_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, perimeter: list[str]
) -> None:
    _dagster(monkeypatch, {product.job_name for product in E2E_INVENTORY.products})
    walkthrough.check_operator_journey_screenshots(e2e_cfg(tmp_path))
    assert perimeter == [
        "Skipping the operator journey screenshots: no product's job selects a Silver table of its own domain, "
        "so no Floe Job would run."
    ]


def test_the_failed_run_is_not_shot_while_its_logs_card_is_still_reading(tmp_path: Path) -> None:
    page = FakePage(set(), statuses={f"/runs/{RUN_ID}": "Reading the run's archived lines…"})
    with pytest.raises(E2EError, match=r"02-failed-run: .*Reading the run's archived lines"):
        _walk(tmp_path, page)
    assert page.saved == ["01-monitoring-overview.png", "02-failed-run.FAILED.png"]


def test_a_status_that_is_not_a_loading_placeholder_does_not_hold_a_shot(tmp_path: Path) -> None:
    page = FakePage(set(), statuses={"": "Trino is starting; queries wait until it answers."})
    _walk(tmp_path, page)
    assert page.saved == [f"{name}.png" for name in walkthrough.screenshot_names(False)]
