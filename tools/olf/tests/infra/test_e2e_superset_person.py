from __future__ import annotations

from typing import Any

import pytest

from olf.infra.e2e import _superset_person as sp
from olf.infra.e2e._shell import E2EError

IDENTITY = {
    "role_mapping": {"platform-admin": ["o:a"], "analyst": ["o:b"], "viewer": ["o:c"]},
    "roles": {
        "precedence": ["platform-admin", "analyst", "viewer"],
        "grants": {"reporting": {"platform-admin": "Admin", "analyst": "Gamma", "viewer": "read-only"}},
        "data_access": {
            "platform-admin": {"dev": {"gold": "write"}, "prod": {"gold": "write"}},
            "analyst": {"dev": {"gold": "read"}, "prod": {"gold": "read"}},
            "viewer": {"prod": {"gold": "read"}},
        },
    },
}
ROUTES = {"stage/dev/reporting": "https://superset-dev.test", "stage/prod/reporting": "https://superset-prod.test"}
LOGINS: dict[str | None, str] = {"platform-admin": "a", "analyst": "b", "viewer": "c", None: "x"}


def test_the_least_privileged_role_that_reads_the_stages_gold_is_chosen() -> None:
    assert sp.reader_role(IDENTITY, "prod") == "viewer"
    assert sp.reader_role(IDENTITY, "dev") == "analyst"
    assert sp.reader_role(IDENTITY, "uat") is None


class _Reply:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        assert self.status_code < 400


class _Browser:
    def __init__(self, data: _Reply) -> None:
        self.data = data
        self.posted: list[str] = []

    def get(self, url: str, **_: Any) -> _Reply:
        if url.endswith("/datasets"):
            return _Reply(200, {"result": [{"id": 7, "type": "table", "table_name": "orders"}]})
        return _Reply(200, {"result": [{"id": 3}]})

    def post(self, url: str, **_: Any) -> _Reply:
        self.posted.append(url)
        return self.data


def _run(data: _Reply, stage: str = "prod") -> list[str]:
    browser = _Browser(data)
    signed_in: list[str] = []

    def sign_in(url: str, login: str) -> tuple[Any, Any]:
        signed_in.append(login)
        return browser, None

    sp.check_person_chart_data(IDENTITY, ROUTES, stage, LOGINS, sign_in)
    return signed_in + browser.posted


def test_rows_from_the_viewers_token_pass() -> None:
    assert _run(_Reply(200, {"result": [{"data": [{"count": 12}]}]})) == [
        "c",
        "https://superset-prod.test/api/v1/chart/data",
    ]


def test_an_oauth2_redirect_instead_of_rows_fails() -> None:
    redirect = _Reply(500, {"errors": [{"error_type": "OAUTH2_REDIRECT"}]})
    with pytest.raises(E2EError, match="OAUTH2_REDIRECT"):
        _run(redirect)


def test_a_stage_no_role_reads_is_skipped() -> None:
    assert _run(_Reply(500, {}), stage="uat") == []


ROWS = _Reply(200, {"result": [{"data": [{"count": 12}]}]})


def _run_on_dev(denied: _Reply) -> list[str]:
    """DEV: the analyst reads Gold, then the viewer, whose grants read none of it, asks for the same chart."""
    replies = {"b": ROWS, "c": denied}
    asked: list[str] = []

    def sign_in(url: str, login: str) -> tuple[Any, Any]:
        asked.append(login)
        return _Browser(replies[login]), None

    sp.check_person_chart_data(IDENTITY, ROUTES, "dev", LOGINS, sign_in)
    return asked


def test_the_role_that_reads_no_gold_on_the_stage_is_the_refused_one() -> None:
    assert sp.refused_role(IDENTITY, "dev") == "viewer"
    assert sp.refused_role(IDENTITY, "prod") is None


def test_a_reader_without_gold_is_refused_the_chart_another_reader_just_read() -> None:
    refused = _Reply(400, {"errors": [{"message": "Forbidden: Principal 'github' is not authorized"}]})

    assert _run_on_dev(refused) == ["b", "c"]


def test_rows_for_the_reader_without_gold_are_an_isolation_breach() -> None:
    with pytest.raises(E2EError, match="isolation breach: viewer read orders on dev"):
        _run_on_dev(ROWS)


def test_a_refusal_that_is_a_lost_sign_in_token_does_not_count() -> None:
    with pytest.raises(E2EError, match="asked to sign in to the database again"):
        _run_on_dev(_Reply(500, {"errors": [{"error_type": "OAUTH2_REDIRECT"}]}))
