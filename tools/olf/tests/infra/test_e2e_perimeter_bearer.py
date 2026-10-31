from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from olf.infra.e2e import _perimeter_bearer as bearer
from olf.infra.e2e._shell import E2EError

URL = "https://dagster-dev.olf.localhost/graphql"
_real_tamper = bearer.tamper_signature
ADMITTED = {"data-engineer", "platform-admin"}


def _server(monkeypatch: pytest.MonkeyPatch, decide: Any) -> list[str | None]:
    seen: list[str | None] = []

    def get(_url: str, headers: dict[str, str], **_kwargs: Any) -> Any:
        token = headers.get("Authorization", "").removeprefix("Bearer ") or None
        seen.append(token)
        return SimpleNamespace(status_code=decide(token))

    monkeypatch.setattr(bearer.requests, "get", get)
    return seen


def _perimeter(token: str | None) -> int:
    """Admits `cli:<role>` for an admitted role; every other token is unauthenticated or below the floor."""
    if token is None or token.startswith(("bad:", "other:")):
        return 401
    return 202 if token.removeprefix("cli:") in ADMITTED else 403


@pytest.fixture(autouse=True)
def _marked_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bearer, "tamper_signature", lambda token: f"bad:{token}")


def _check(other: Any = lambda role: f"other:{role}") -> None:
    bearer.check_bearer_decisions(
        URL,
        ["platform-admin", "data-engineer", "analyst"],
        lambda role: role in ADMITTED,
        lambda role: f"cli:{role}",
        other,
    )


def test_the_floor_decides_for_a_cli_token_and_every_other_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _server(monkeypatch, _perimeter)
    _check()
    assert None in seen and "other:platform-admin" in seen and "bad:cli:platform-admin" in seen


@pytest.mark.parametrize(
    ("decide", "match"),
    [
        (lambda token: 202, "below the floor, expected 403"),
        (lambda token: 403, "at or above the floor, expected 2xx"),
        (lambda token: 202 if token is None else _perimeter(token), "no token"),
        (lambda token: 202 if token and token.startswith("other:") else _perimeter(token), "another client"),
    ],
)
def test_a_wrong_decision_fails_the_check(monkeypatch: pytest.MonkeyPatch, decide: Any, match: str) -> None:
    _server(monkeypatch, decide)
    with pytest.raises(E2EError, match=match):
        _check()


def test_a_tampered_signature_that_is_accepted_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _server(monkeypatch, lambda token: 202 if token and token.startswith("bad:") else _perimeter(token))
    with pytest.raises(E2EError, match="tampered"):
        _check()


def test_the_other_client_case_is_left_out_without_a_token_source(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _server(monkeypatch, _perimeter)
    _check(other=None)
    assert not any(token and token.startswith("other:") for token in seen)


def test_tampering_changes_the_signature_but_keeps_the_token_shape() -> None:
    token = "aaa.bbb.cccccccccc"
    tampered = _real_tamper(token)
    assert tampered != token and tampered.startswith("aaa.bbb.") and len(tampered) == len(token)
    assert _real_tamper("a.b.cAcccc") != "a.b.cAcccc"


def _api_check(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    monkeypatch.setattr(bearer.requests, "get", lambda url, headers, **kw: _get(paths, answers, url, headers))
    monkeypatch.setattr(bearer.requests, "post", lambda url, headers, **kw: _get(paths, answers, url, headers))
    bearer.check_platform_api(
        "https://olf.localhost",
        ["platform-admin", "viewer"],
        lambda role: f"cli:{role}",
        lambda r: f"other:{r}",
        stage="dev",
        precedence=["platform-admin", "data-engineer", "analyst", "viewer"],
    )
    return paths


def _get(paths: list[str], answers: dict[str, Any], url: str, headers: dict[str, str]) -> Any:
    """A perimeter-fronted API: `answers` maps a path to (status, body) for a `cli:<role>` token's role."""
    path = url.removeprefix("https://olf.localhost").split("?", 1)[0]
    paths.append(path)
    token = headers.get("Authorization", "").removeprefix("Bearer ") or None
    if token is None or token.startswith(("bad:", "other:")):
        return SimpleNamespace(status_code=401, json=dict, text="")
    status, body = answers[path](token.removeprefix("cli:"))
    return SimpleNamespace(status_code=status, json=lambda: body, text=str(body))


HEALTHY = {
    "/api/healthz": lambda role: (200, {"status": "ok"}),
    "/api/v1/whoami": lambda role: (200, {"role": role}),
    # The matrix's data-engineer floor; platform-admin is above it, viewer below.
    "/api/v1/stages/dev/logs": lambda role: (200, {"lines": []}) if role == "platform-admin" else (403, {}),
    # The launch route's data-engineer floor: an absent job is 404 once the attempt is stored.
    "/api/v1/stages/dev/runs": lambda role: (404, {}) if role == "platform-admin" else (403, {}),
}


def test_the_platform_api_answers_each_role_through_the_perimeter(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _api_check(monkeypatch, HEALTHY)
    assert "/api/healthz" in paths and paths.count("/api/v1/whoami") >= 2
    assert paths.count("/api/v1/stages/dev/logs") >= 2
    assert paths.count("/api/v1/stages/dev/runs") == 2


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"/api/healthz": lambda role: (502, {})}, "healthz"),
        ({"/api/v1/whoami": lambda role: (200, {"role": "viewer"})}, "whoami for platform-admin"),
        ({"/api/v1/whoami": lambda role: (403, {})}, "whoami"),
        # The object store refusing the API's key, or a viewer let through.
        ({"/api/v1/stages/dev/logs": lambda role: (502, {})}, "platform-admin.*stages/dev/logs.*502"),
        ({"/api/v1/stages/dev/logs": lambda role: (200, {})}, "viewer.*stages/dev/logs.*200"),
        # The API could not store the attempt, or a viewer reached the job lookup.
        ({"/api/v1/stages/dev/runs": lambda role: (503, {})}, "platform-admin launching.*503.*not stored"),
        ({"/api/v1/stages/dev/runs": lambda role: (404, {})}, "viewer launching.*expected HTTP 403, got 404"),
    ],
)
def test_a_wrong_platform_api_answer_fails(monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], match: str) -> None:
    with pytest.raises(E2EError, match=match):
        _api_check(monkeypatch, {**HEALTHY, **change})


def test_on_a_stage_outside_the_launch_row_every_role_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses: list[tuple[str, int]] = []

    def post(url: str, headers: dict[str, str], **kw: Any) -> Any:
        assert url == "https://olf.localhost/api/v1/stages/prod/runs" and kw["json"] == {"job": bearer.ABSENT_JOB}
        statuses.append((headers["Authorization"], 403))
        return SimpleNamespace(status_code=403)

    monkeypatch.setattr(bearer.requests, "post", post)
    bearer.check_launch_audit(
        "https://olf.localhost",
        ["platform-admin", "viewer"],
        lambda role: f"cli:{role}",
        stage="prod",
        precedence=["platform-admin", "data-engineer", "analyst", "viewer"],
    )
    assert [auth for auth, _ in statuses] == ["Bearer cli:platform-admin", "Bearer cli:viewer"]
