"""`olf health` and `olf pipelines health` against a fake platform API: what is shown, what is passed through, and
the exit code each state earns."""

from __future__ import annotations

import json
from typing import Any

import pytest
import test_reads
from test_reads import ANSWERS, FakeAPI, _olf

from olf.platform import api

fake = test_reads.fake  # the fake platform API fixture

NOW = "2026-11-16T09:00:00Z"


def _signal(state: str, reason: str) -> dict[str, Any]:
    return {
        "kind": "execution",
        "state": state,
        "reason": reason,
        "observed_at": NOW,
        "expires_at": None,
        "run": None,
    }


def _pipeline(job: str, state: str, reason: str) -> dict[str, Any]:
    return {
        "job": job,
        "state": state,
        "failed_runs": 0,
        "freshness": {"age_seconds": None, "last_success_at": None},
        "last_success": None,
        "latest_run": None,
        "schedule": {},
        "signals": [_signal("healthy", "ran"), _signal(state, reason)] if state != "healthy" else [],
    }


def _stage(state: str) -> dict[str, Any]:
    return {
        "stage": "dev",
        "state": state,
        "evaluated_at": NOW,
        "services": [
            {
                "component": "trino",
                "state": "healthy",
                "reason": "probe answered",
                "observed_at": NOW,
                "expires_at": None,
            },
            {
                "component": "polaris",
                "state": state,
                "reason": f"polaris is {state}",
                "observed_at": None,
                "expires_at": None,
            },
        ],
        "pipelines": [{"job": "orders_job", "state": state}],
        "integrations": [{"name": "quality", "status": "unavailable", "reason": "Floe reports are not read"}],
    }


def _serve(monkeypatch: pytest.MonkeyPatch, stage: str, pipelines: list[dict[str, Any]]) -> None:
    monkeypatch.setitem(ANSWERS, "/api/v1/stages/dev/health", _stage(stage))
    monkeypatch.setitem(
        ANSWERS,
        "/api/v1/stages/dev/health/pipelines",
        {"stage": "dev", "evaluated_at": NOW, "integrations": [], "pipelines": pipelines, "thresholds": {}},
    )


@pytest.mark.parametrize(
    ("state", "exit_code"),
    [("healthy", 0), ("warning", 0), ("disabled", 0), ("critical", 1), ("unknown", 1)],
)
def test_the_exit_code_follows_the_stages_state(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch, state: str, exit_code: int
) -> None:
    _serve(monkeypatch, state, [])

    result = _olf("health", "dev")

    assert result.exit_code == exit_code, result.output
    assert f"Stage dev: {state}" in result.stdout
    assert fake.requests[0]["path"] == "/api/v1/stages/dev/health"
    assert fake.requests[0]["authorization"] == f"Bearer {fake.token}"


def test_health_shows_each_service_and_pipeline_and_the_unavailable_integration(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, "critical", [])

    result = _olf("health", "dev")

    assert "polaris | critical | polaris is critical" in result.stdout
    assert "trino   | healthy  | probe answered" in result.stdout
    assert "orders_job | critical" in result.stdout
    assert "quality is unavailable: Floe reports are not read" in result.stdout


def test_an_unknown_stage_never_prints_healthy(fake: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ANSWERS, "/api/v1/stages/dev/health", {**_stage("unknown"), "services": [], "pipelines": []})

    result = _olf("health", "dev")

    assert result.exit_code == 1
    assert "healthy" not in result.stdout


@pytest.mark.parametrize(
    ("command", "path"),
    [
        (("health", "dev"), "/api/v1/stages/dev/health"),
        (("pipelines", "health", "dev"), "/api/v1/stages/dev/health/pipelines"),
    ],
)
def test_json_passes_the_apis_response_through_unchanged(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...], path: str
) -> None:
    _serve(monkeypatch, "healthy", [_pipeline("orders_job", "warning", "stale")])

    result = _olf(*command, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == ANSWERS[path]


def test_pipelines_health_shows_the_reason_of_the_signal_behind_each_state(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(
        monkeypatch,
        "healthy",
        [_pipeline("orders_job", "warning", "no success for 30h"), _pipeline("ok_job", "healthy", "")],
    )

    result = _olf("pipelines", "health", "dev")

    assert result.exit_code == 0, result.output
    assert "orders_job | warning | no success for 30h" in result.stdout
    assert "ok_job     | healthy | -" in result.stdout


@pytest.mark.parametrize("state", ["critical", "unknown"])
def test_pipelines_health_fails_when_a_pipeline_is_critical_or_unknown(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _serve(monkeypatch, "healthy", [_pipeline("orders_job", state, "no signal")])

    result = _olf("pipelines", "health", "dev")

    assert result.exit_code == 1
    assert f"orders_job | {state}" in result.stdout


@pytest.mark.parametrize("command", [("health", "dev"), ("pipelines", "health", "dev")])
def test_an_unreachable_api_exits_1_and_says_so(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...]
) -> None:
    fake.server.shutdown()
    fake.server.server_close()
    monkeypatch.setattr(api, "base_url", lambda instance: "http://127.0.0.1:1/api")

    result = _olf(*command)

    assert result.exit_code == 1
    assert "cannot reach the platform API" in result.stderr


def test_a_stage_the_api_does_not_know_is_reported(fake: FakeAPI) -> None:
    fake.refuse(404, "stage 'qa' is not enabled")

    result = _olf("health", "qa")

    assert result.exit_code == 1
    assert "stage 'qa' is not enabled" in result.stderr
