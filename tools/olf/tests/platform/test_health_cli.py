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
EXPIRES = "2026-11-16T09:01:00Z"


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


WRITTEN = "2026-11-16T08:55:00Z"


def _service(component: str, state: str, service: str, name: str, display: str, **fields: Any) -> dict[str, Any]:
    return {
        "component": component,
        "source": "kubernetes",
        "state": state,
        "reason": f"{component} is {state}",
        "observed_at": NOW,
        "expires_at": EXPIRES,
        "max_age_seconds": 60,
        "display_name": display,
        "service": service,
        "service_display_name": name,
        "last_success_at": None,
        **fields,
    }


def _stage(state: str) -> dict[str, Any]:
    return {
        "stage": "dev",
        "state": state,
        "evaluated_at": NOW,
        "services": [
            _service("trino.workloads", "healthy", "trino", "Trino", "Trino workloads", reason="probe answered"),
            _service(
                "polaris.readiness",
                state,
                "polaris",
                "Polaris",
                "Polaris catalog",
                observed_at=None,
                expires_at=None,
            ),
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

    assert "Polaris: critical" in result.stdout
    assert "polaris.readiness | Polaris catalog | critical | never" in result.stdout
    assert "polaris.readiness is critical" in result.stdout
    assert "Trino: healthy" in result.stdout
    assert _row(result.stdout, "trino.workloads")[:4] == ["trino.workloads", "Trino workloads", "healthy", NOW]
    assert EXPIRES in result.stdout
    assert "probe answered" in result.stdout
    assert "orders_job | critical" in result.stdout
    assert "quality is unavailable: Floe reports are not read" in result.stdout


def _services_only(monkeypatch: pytest.MonkeyPatch, services: list[dict[str, Any]]) -> None:
    monkeypatch.setitem(ANSWERS, "/api/v1/stages/dev/health", {**_stage("warning"), "services": services})


def _row(stdout: str, component: str) -> list[str]:
    line = next(line for line in stdout.splitlines() if line.startswith(component + " "))
    return [cell.strip() for cell in line.split("|")]


def _heading_of(stdout: str, component: str) -> str:
    # The heading a component's row sits under: the last `<service>: <state>` line above it.
    lines = stdout.splitlines()
    row = next(i for i, line in enumerate(lines) if line.startswith(component + " "))
    return next(line for line in reversed(lines[:row]) if ": " in line and "|" not in line)


def test_services_are_grouped_under_their_service_headed_by_the_worst_state(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ("log-archive", "Log archive")
    _services_only(
        monkeypatch,
        [
            _service("log-archive.spool-age", "healthy", *log, "Log spool age"),
            _service("trino.readiness", "healthy", "trino", "Trino", "Trino coordinator"),
            _service("log-archive.spool-size", "warning", *log, "Log spool size"),
            _service("log-archive.newest-object", "disabled", *log, "Newest archived log"),
        ],
    )

    result = _olf("health", "dev")

    assert result.exit_code == 0, result.output
    assert result.stdout.count("Log archive: ") == 1
    for component in ("log-archive.spool-age", "log-archive.spool-size", "log-archive.newest-object"):
        assert _heading_of(result.stdout, component) == "Log archive: warning"
    assert _heading_of(result.stdout, "trino.readiness") == "Trino: healthy"
    assert _row(result.stdout, "log-archive.spool-size")[1] == "Log spool size"


def test_an_unknown_component_outranks_its_healthy_neighbours_in_the_heading(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _services_only(
        monkeypatch,
        [
            _service("dagster.workloads", "healthy", "dagster", "Dagster", "Dagster workloads"),
            _service("dagster.read-api", "unknown", "dagster", "Dagster", "Dagster API", observed_at=None),
        ],
    )

    result = _olf("health", "dev")

    assert _heading_of(result.stdout, "dagster.workloads") == "Dagster: unknown"
    assert "Dagster: healthy" not in result.stdout


def test_last_write_shows_the_durable_write_and_a_dash_where_there_is_none(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ("log-archive", "Log archive")
    _services_only(
        monkeypatch,
        [
            _service("log-archive.newest-object", "healthy", *log, "Newest archived log", last_success_at=WRITTEN),
            _service("log-archive.spool-age", "healthy", *log, "Log spool age"),
        ],
    )

    result = _olf("health", "dev")

    assert _row(result.stdout, "log-archive.newest-object")[5] == WRITTEN
    # Never the probe time in place of a write the API did not report.
    assert _row(result.stdout, "log-archive.spool-age")[3:6] == [NOW, EXPIRES, "\u2014"]


def test_rows_from_an_api_without_display_fields_are_shown_honestly(
    fake: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = {
        "component": "polaris",
        "state": "critical",
        "reason": "polaris is critical",
        "observed_at": None,
        "expires_at": None,
    }
    _services_only(monkeypatch, [legacy])

    result = _olf("health", "dev")

    assert result.exit_code == 0, result.output
    assert _heading_of(result.stdout, "polaris") == "(service not reported): critical"
    assert _row(result.stdout, "polaris") == [
        "polaris",
        "polaris",
        "critical",
        "never",
        "-",
        "\u2014",
        "polaris is critical",
    ]


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
