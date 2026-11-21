from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from olf.infra import access
from olf.infra.e2e import _identity_github
from olf.infra.e2e._identity_github import dex_discovery_answer, terminating_pods, wait_until_settled
from olf.infra.e2e._shell import E2EError


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.now += seconds


def sequence(*values: Any) -> Any:
    """Answers each value in turn, then repeats the last one."""
    remaining = list(values)

    def next_value() -> Any:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return next_value


def test_waits_for_old_pods_to_drain_then_for_prompt_answers_in_a_row() -> None:
    clock = Clock()
    probed_while_draining: list[bool] = []
    terminating = sequence(["dex-old"], ["dex-old", "perimeter-old"], [])
    answers = sequence("ReadTimeout from https://auth/x", None, "ReadTimeout from https://auth/x", None, None, None)

    def answer() -> str | None:
        probed_while_draining.append(bool(pods_now))
        return answers()

    pods_now: list[str] = []

    def pods() -> list[str]:
        pods_now[:] = terminating()
        return pods_now

    wait_until_settled(pods, answer, what="Dex", clock=clock, pause=clock.pause)

    assert not any(probed_while_draining)
    # Two draining polls, then six probes: one slow, one fast, one slow, three fast in a row.
    assert len(probed_while_draining) == 6
    assert clock.now == 7.0


def test_expiry_names_the_pods_still_terminating() -> None:
    clock = Clock()
    with pytest.raises(E2EError, match=r"within 10s; pods still terminating: dex-a, dex-b"):
        wait_until_settled(
            lambda: ["dex-b", "dex-a"], lambda: None, what="Dex", timeout=10, clock=clock, pause=clock.pause
        )


def test_expiry_names_the_last_slow_answer() -> None:
    clock = Clock()
    with pytest.raises(E2EError, match=r"Dex through the edge: ReadTimeout from https://auth"):
        wait_until_settled(
            lambda: [],
            sequence(None, "ReadTimeout from https://auth"),
            what="Dex through the edge",
            timeout=10,
            clock=clock,
            pause=clock.pause,
        )


def test_expiry_while_answers_have_not_yet_reached_the_streak() -> None:
    clock = Clock()
    with pytest.raises(E2EError, match=r"answered 2 of 3 times in a row"):
        wait_until_settled(
            lambda: [], sequence("slow", None, None, "slow"), what="Dex", timeout=2, clock=clock, pause=clock.pause
        )


def test_terminating_pods_reads_each_deployment_selector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deployments = {
        ("deployment/dex",): {"kind": "Deployment", "spec": {"selector": {"matchLabels": {"app": "dex"}}}},
        ("deployment", "openlakeforge.io/component=perimeter"): {
            "kind": "List",
            "items": [{"spec": {"selector": {"matchLabels": {"app": "oauth2-proxy", "tier": "edge"}}}}],
        },
    }
    pods = {
        "app=dex": [
            {"metadata": {"name": "dex-new"}},
            {"metadata": {"name": "dex-old", "deletionTimestamp": "2026-11-21T00:31:48Z"}},
        ],
        "app=oauth2-proxy,tier=edge": [{"metadata": {"name": "proxy-new"}}],
    }

    def fake_kubectl(_cfg: object, args: list[str], **_kwargs: object) -> str:
        assert args[2:4] == ["-n", "shared"]
        if args[1] == "pods":
            return json.dumps({"items": pods[args[args.index("-l") + 1]]})
        key = (args[1], args[args.index("-l") + 1]) if "-l" in args else (args[1],)
        return json.dumps(deployments[key])

    monkeypatch.setattr(_identity_github, "kubectl", fake_kubectl)
    restarts = [("deployment/dex", None), ("deployment", "openlakeforge.io/component=perimeter")]
    assert terminating_pods(object(), "shared", restarts) == ["dex-old"]  # type: ignore[arg-type]  # the fake kubectl ignores cfg


class FakeSession:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, *, timeout: float, allow_redirects: bool) -> Any:
        self.calls.append((url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        reply = requests.Response()
        reply.status_code = outcome
        return reply


def test_discovery_answer_reports_slow_and_failed_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([requests.exceptions.ReadTimeout(), 502, 200])
    monkeypatch.setattr(access.Route, "session", lambda _self: session)
    answer = dex_discovery_answer(access.Route("https://auth.olf.localhost"), "https://auth.olf.localhost/")
    url = "https://auth.olf.localhost/.well-known/openid-configuration"
    assert answer() == f"ReadTimeout from {url}"
    assert answer() == f"HTTP 502 from {url}"
    assert answer() is None
    assert session.calls == [(url, 3.0)] * 3


def test_discovery_answer_does_not_hide_a_trust_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access.Route, "session", lambda _self: FakeSession([requests.exceptions.SSLError("bad")]))
    answer = dex_discovery_answer(access.Route("https://auth.olf.localhost"), "https://auth.olf.localhost")
    with pytest.raises(requests.exceptions.SSLError):
        answer()
