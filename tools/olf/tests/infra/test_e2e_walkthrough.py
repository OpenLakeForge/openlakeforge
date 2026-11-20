from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import e2e_cfg
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


def test_the_journey_needs_the_drill_downs_failed_run_as_the_latest() -> None:
    assert walkthrough.journey_failed_run([_run()]).run_id == RUN_ID
    with pytest.raises(E2EError, match="runs before this check"):
        walkthrough.journey_failed_run([])
    with pytest.raises(E2EError, match="is SUCCESS"):
        walkthrough.journey_failed_run([_run("SUCCESS")])
    with pytest.raises(E2EError, match="not the drill-down's failed run"):
        walkthrough.journey_failed_run([_run(tags={})])


def test_the_metrics_shot_is_named_for_what_it_shows() -> None:
    assert walkthrough.screenshot_names(True)[4] == "05-metrics"
    assert walkthrough.screenshot_names(False)[4] == "05-metrics-off"
    assert len(walkthrough.screenshot_names(True)) == 6


class FakeLocator:
    def __init__(self, page: FakePage, path: tuple[str, ...]) -> None:
        self.page, self.path = page, path

    def get_by_role(self, role: str, **kwargs: Any) -> FakeLocator:
        return FakeLocator(self.page, (*self.path, f"{role}:{kwargs.get('name', '')}"))

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, _index: int) -> FakeLocator:
        return self

    def click(self) -> None:
        if any("Open run" in part for part in self.path):
            self.page.url = f"{PORTAL}/monitoring/lakehouse/runs/{RUN_ID}"

    def check(self) -> None:
        pass

    def scroll_into_view_if_needed(self) -> None:
        pass

    def screenshot(self, path: str) -> None:
        self.page.saved.append(Path(path).name)


class FakePage(FakeLocator):
    """A page on which every locator is there, except those whose accessible names `missing` lists."""

    def __init__(self, missing: set[str], shown: str = "", saved: list[str] | None = None) -> None:
        super().__init__(self, ())
        self.missing, self.shown, self.url = missing, shown, PORTAL
        self.saved = [] if saved is None else saved

    def goto(self, url: str) -> None:
        self.url = url

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
        phone = FakeContext(FakePage(self.page.missing, saved=self.page.saved))
        phone.options = options
        self.options = options
        return phone

    def close(self) -> None:
        pass


class FakePlaywright:
    expect = FakeExpect


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
    )
    assert page.saved == [f"{name}.png" for name in walkthrough.screenshot_names(False)]
    assert context.options["viewport"] == walkthrough.PHONE and context.options["device_scale_factor"] == 2


def test_a_page_that_missed_its_state_is_kept_as_failed_and_fails_the_walk(tmp_path: Path) -> None:
    page = FakePage({"Quality check results"})
    with pytest.raises(E2EError, match=r"04-run-quality: the page did not reach its expected state \(kept as FAILED\)"):
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
    assert "secret scan of the page could not complete (RuntimeError)" in str(raised.value)
    assert SECRET not in str(raised.value)


def test_a_page_showing_a_secret_value_is_not_saved(tmp_path: Path) -> None:
    page = FakePage(set(), shown="token s3cr3t-value-0123456789")
    with pytest.raises(E2EError, match="01-monitoring-overview: .*no screenshot saved"):
        _walk(tmp_path, page)
    assert page.saved == []


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
