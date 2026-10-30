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
