"""The GitHub Discussions notifier against a fake GitHub that answers in the shapes the REST and GraphQL APIs use."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from openlakeforge_services.notify import MAX_TOTAL_SECONDS

from openlakeforge_platform_api.alerting.github_discussions import (
    GitHubDiscussionsNotifier,
    delivery_marker,
    graphql_url,
    incident_marker,
    run_marker,
)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_PEM = (
    KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
)
TOKEN = "ghs_installation_token_value"
START = 1_800_000_000.0

OPENED = {
    "schema": "openlakeforge.alert/v1",
    "event": "opened",
    "incident_id": "inc-1",
    "condition": "run_failed",
    "severity": "critical",
    "stage": "dev",
    "domain": "sales",
    "product": "order_revenue",
    "run_id": "run-9",
    "opened_at": "2026-11-25T10:00:00Z",
    "event_at": "2026-11-25T10:00:00Z",
    "summary": "order_revenue failed in dev",
    "links": {"run": "https://dagster.example/runs/run-9"},
}


def url_of(discussion_id: str) -> str:
    return f"https://github.com/acme/ops-alerts/discussions/{discussion_id}"


def reply(status: int, body: object = None, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body if body is not None else {}).encode()
    response.headers.update(headers or {})
    return response


def graphql_errors(*types: str, status: int = 200) -> requests.Response:
    return reply(status, {"data": None, "errors": [{"type": t, "message": "internal detail"} for t in types]})


class FakeGitHub:
    """A `requests.Session` stand-in for one repository; `faults[route]` is answered first, once each."""

    def __init__(self, category: str = "Alerts") -> None:
        self.calls: list[dict[str, Any]] = []
        self.discussions: list[dict[str, Any]] = []
        self.faults: dict[str, Sequence[requests.Response | Exception]] = {}
        self.category = category
        self.token_lifetime = 3600
        self.now = START
        self.created = 0

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("POST", url, kwargs)

    def of(self, route: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["route"] == route]

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> requests.Response:
        body = kwargs.get("json")
        query = body.get("query", "") if body else ""
        route = self._route(method, url, query)
        self.calls.append({"route": route, "url": url, "json": body, "headers": kwargs["headers"], "kwargs": kwargs})
        queue = list(self.faults.get(route, ()))
        if queue:
            fault = queue.pop(0)
            self.faults[route] = queue
            if isinstance(fault, Exception):
                raise fault
            return fault
        return getattr(self, f"_{route}")(body)

    @staticmethod
    def _route(method: str, url: str, query: str) -> str:
        if method == "GET":
            return "installation"
        if "/access_tokens" in url:
            return "token"
        for name, needle in (
            ("create", "createDiscussion"),
            ("comment", "addDiscussionComment"),
            ("close", "closeDiscussion"),
            ("repository", "hasDiscussionsEnabled"),
        ):
            if needle in query:
                return name
        return "recent"

    def _installation(self, body: Any) -> requests.Response:
        return reply(200, {"id": 42, "app_id": 7})

    def _token(self, body: Any) -> requests.Response:
        expires = datetime.fromtimestamp(self.now + self.token_lifetime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return reply(201, {"token": TOKEN, "expires_at": expires})

    def _repository(self, body: Any) -> requests.Response:
        categories = [{"id": "C_alerts", "name": self.category, "slug": self.category.lower()}]
        repository = {
            "id": "R_1",
            "hasDiscussionsEnabled": True,
            "discussionCategories": {"nodes": [{"id": "C_other", "name": "General", "slug": "general"}, *categories]},
        }
        return reply(200, {"data": {"repository": repository}})

    def _recent(self, body: Any) -> requests.Response:
        nodes = [
            {
                "id": d["id"],
                "url": url_of(d["id"]),
                "body": d["body"],
                "closed": d["closed"],
                "viewerDidAuthor": not d.get("foreign", False),
                "comments": {
                    "nodes": [
                        *({"body": c, "viewerDidAuthor": True} for c in d["comments"]),
                        *({"body": c, "viewerDidAuthor": False} for c in d.get("foreign_comments", [])),
                    ][-20:]
                },
            }
            for d in reversed(self.discussions[-50:])
        ]
        return reply(200, {"data": {"repository": {"discussions": {"nodes": nodes}}}})

    def _create(self, body: Any) -> requests.Response:
        self.created += 1
        variables = body["variables"]
        discussion = {
            "id": f"D_{self.created}",
            "title": variables["title"],
            "body": variables["body"],
            "closed": False,
            "comments": [],
            "category": variables["category"],
            "repository": variables["repository"],
        }
        self.discussions.append(discussion)
        created = {"id": discussion["id"], "url": url_of(discussion["id"])}
        return reply(200, {"data": {"createDiscussion": {"discussion": created}}})

    def _comment(self, body: Any) -> requests.Response:
        self.by_id(body["variables"]["discussion"])["comments"].append(body["variables"]["body"])
        return reply(200, {"data": {"addDiscussionComment": {"comment": {"id": "DC_1"}}}})

    def _close(self, body: Any) -> requests.Response:
        self.by_id(body["variables"]["discussion"])["closed"] = True
        return reply(200, {"data": {"closeDiscussion": {"discussion": {"id": "x"}}}})

    def by_id(self, discussion_id: str) -> dict[str, Any]:
        return next(d for d in self.discussions if d["id"] == discussion_id)


class Env:
    def __init__(self, github: FakeGitHub, notifier: GitHubDiscussionsNotifier, sleeps: list[float]) -> None:
        self.github = github
        self.notifier = notifier
        self.sleeps = sleeps


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def make(github: FakeGitHub) -> Callable[..., Env]:
    def build(**overrides: Any) -> Env:
        sleeps: list[float] = []
        settings: dict[str, Any] = {
            "repository": "acme/ops-alerts",
            "category": "alerts",
            "app_id": "12345",
            "private_key": PRIVATE_PEM,
            "session": github,
            "clock": lambda: github.now,
            "sleep": sleeps.append,
            "jitter": lambda ceiling: 0.0,
        }
        settings.update(overrides)
        return Env(github, GitHubDiscussionsNotifier("team", **settings), sleeps)

    return build


def event(name: str, **fields: Any) -> dict[str, Any]:
    return {**OPENED, "event": name, **fields}


def test_opened_creates_one_discussion_carrying_both_markers(github: FakeGitHub, make: Callable[..., Env]) -> None:
    result = make().notifier.send(OPENED, delivery_id="n-1")

    assert (result.status, result.attempts, result.url) == ("delivered", 1, url_of("D_1"))
    (discussion,) = github.discussions
    assert discussion["title"] == "[critical] run failed · dev · sales/order_revenue"
    assert discussion["category"] == "C_alerts"
    assert discussion["repository"] == "R_1"
    assert incident_marker("inc-1") in discussion["body"]
    assert delivery_marker("n-1") in discussion["body"]
    assert run_marker("dev", "run-9") in discussion["body"]
    assert "| run id | run-9 |" in discussion["body"]
    assert discussion["closed"] is False


def test_the_body_leads_with_one_sentence_then_when_and_where_to_look(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    links = {
        "runbook": "https://docs.example/alerts.md#run_failed",
        "run": "https://dagster.example/runs/run-9",
        "portal": "https://lake.example/monitoring?stage=dev&incident=inc-1",
    }
    make().notifier.send({**OPENED, "event_at": "2026-11-25T10:00:01.830169+00:00", "links": links}, delivery_id="n-1")

    lines = github.discussions[0]["body"].splitlines()
    assert lines[0] == "🔴 **Critical** · **sales/order_revenue** in **dev**: order_revenue failed in dev."
    assert lines[2] == "Opened 2026-11-25 10:00 UTC · run `run-9`"
    assert lines[4] == (
        "[Open in the Portal](https://lake.example/monitoring?stage=dev&incident=inc-1)"
        " · [Dagster run](https://dagster.example/runs/run-9)"
        " · [Runbook](https://docs.example/alerts.md#run_failed)"
    )
    # The fields stay for whoever needs them, folded, with times to the minute.
    assert lines[6] == "<details><summary>Details</summary>"
    assert "| event at | 2026-11-25 10:00 UTC |" in lines
    assert not any(line.startswith("| event |") for line in lines)


def test_a_platform_condition_leads_with_its_stage(github: FakeGitHub, make: Callable[..., Env]) -> None:
    degraded = {
        "event": "opened",
        "incident_id": "inc-s",
        "condition": "service_degraded",
        "severity": "warning",
        "stage": "dev",
        "summary": "Dagster workloads are degraded",
        "opened_at": "2026-11-26T09:35:01+00:00",
    }
    make().notifier.send(degraded, delivery_id="n-1")

    assert github.discussions[0]["body"].splitlines()[0] == "🟠 **Warning** · **dev**: Dagster workloads are degraded."


def test_the_title_leaves_out_what_the_payload_lacks(github: FakeGitHub, make: Callable[..., Env]) -> None:
    sparse = {"event": "opened", "incident_id": "inc-2", "condition": "stale_data", "stage": "prod"}
    make().notifier.send(sparse, delivery_id="n-1")

    assert github.discussions[0]["title"] == "stale data · prod"


def test_resending_a_delivery_creates_nothing(github: FakeGitHub, make: Callable[..., Env]) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")
    writes = len(github.of("create"))

    result = env.notifier.send(OPENED, delivery_id="n-1")

    assert (result.status, result.url) == ("delivered", url_of("D_1"))
    assert len(github.of("create")) == writes == 1


def test_a_new_notifier_instance_also_recognises_a_posted_delivery(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    make().notifier.send(OPENED, delivery_id="n-1")

    assert make().notifier.send(OPENED, delivery_id="n-1").status == "delivered"
    assert len(github.discussions) == 1


def test_escalated_comments_on_the_incidents_discussion(github: FakeGitHub, make: Callable[..., Env]) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")
    env.notifier.send(event("opened", incident_id="inc-other", run_id="run-other"), delivery_id="n-other")

    escalated = event(
        "escalated", severity="critical", event_at="2026-11-25T10:20:00Z", summary="a blocking quality check failed"
    )
    result = env.notifier.send(escalated, delivery_id="n-2")

    assert (result.status, result.url) == ("delivered", url_of("D_1"))
    assert len(github.discussions) == 2
    first, other = github.discussions
    assert len(first["comments"]) == 1 and delivery_marker("n-2") in first["comments"][0]
    assert first["comments"][0].splitlines()[0] == (
        "⬆️ **Escalated to critical** at 10:20 UTC: a blocking quality check failed."
    )
    assert other["comments"] == []
    assert first["closed"] is False

    env.notifier.send(event("escalated"), delivery_id="n-2")
    assert len(first["comments"]) == 1


def test_resolved_comments_then_closes_as_resolved(github: FakeGitHub, make: Callable[..., Env]) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")

    result = env.notifier.send(event("resolved", event_at="2026-11-25T11:25:00Z"), delivery_id="n-3")

    assert result.status == "delivered"
    (discussion,) = github.discussions
    (comment,) = discussion["comments"]
    assert delivery_marker("n-3") in comment
    # One line: when, and for how long. Nothing the opening post already said.
    assert comment.splitlines()[0] == "✅ **Resolved** at 11:25 UTC, after 1 h 25 min."
    assert "<details>" not in comment and "|" not in comment
    assert discussion["closed"] is True
    assert "reason: RESOLVED" in github.of("close")[0]["json"]["query"]


def test_a_resolved_whose_close_failed_is_finished_by_the_retry_without_a_second_comment(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")
    github.faults["close"] = [reply(502)]

    result = env.notifier.send(event("resolved"), delivery_id="n-3")

    assert (result.status, result.attempts) == ("delivered", 2)
    (discussion,) = github.discussions
    assert len(discussion["comments"]) == 1
    assert discussion["closed"] is True


@pytest.mark.parametrize("name", ["escalated", "resolved"])
def test_a_missing_discussion_leads_to_a_new_one_titled_with_the_event(
    github: FakeGitHub, make: Callable[..., Env], name: str
) -> None:
    result = make().notifier.send(event(name), delivery_id="n-2")

    assert result.status == "delivered"
    (discussion,) = github.discussions
    assert discussion["title"] == f"{name}: [critical] run failed · dev · sales/order_revenue"
    assert incident_marker("inc-1") in discussion["body"]
    assert discussion["closed"] is (name == "resolved")


def test_an_unknown_event_creates_a_discussion_titled_with_it(github: FakeGitHub, make: Callable[..., Env]) -> None:
    make().notifier.send(event("silenced"), delivery_id="n-4")

    assert github.discussions[0]["title"].startswith("silenced: ")


def test_a_test_event_creates_a_discussion_and_closes_it(github: FakeGitHub, make: Callable[..., Env]) -> None:
    test_event = {"schema": "openlakeforge.alert/v1", "event": "test", "stage": "dev", "summary": "hello"}

    result = make().notifier.send(test_event, delivery_id="t-1")

    assert result.status == "delivered"
    (discussion,) = github.discussions
    assert discussion["title"] == "[test] OpenLakeForge alert test · dev"
    assert discussion["body"].startswith("🧪 A test notification for stage **dev**, sent by `olf infra alerts test`.")
    assert delivery_marker("t-1") in discussion["body"]
    assert discussion["closed"] is True


QUALITY = {
    **OPENED,
    "incident_id": "inc-q",
    "condition": "quality",
    "summary": "a blocking quality check failed",
    "links": {"runbook": "https://docs.example/alerts.md#quality"},
}


def test_a_second_incident_of_the_same_run_joins_the_first_ones_discussion(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")

    result = env.notifier.send(QUALITY, delivery_id="q-1")

    assert (result.status, result.url) == ("delivered", url_of("D_1"))
    (discussion,) = github.discussions
    (comment,) = discussion["comments"]
    assert comment.splitlines()[0] == "🔴 **Quality** (critical) on the same run: a blocking quality check failed."
    assert incident_marker("inc-q") in comment and delivery_marker("q-1") in comment

    # Its later events find it there too.
    env.notifier.send({**QUALITY, "event": "escalated"}, delivery_id="q-2")
    assert len(github.discussions) == 1 and len(discussion["comments"]) == 2
    assert discussion["comments"][1].startswith("⬆️ **Quality escalated to critical**")


def test_a_shared_discussion_closes_only_once_every_incident_in_it_resolved(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")
    env.notifier.send(QUALITY, delivery_id="q-1")
    (discussion,) = github.discussions

    env.notifier.send({**QUALITY, "event": "resolved", "event_at": "2026-11-25T10:05:00Z"}, delivery_id="q-3")

    assert discussion["comments"][-1].splitlines()[0] == "✅ **Quality resolved** at 10:05 UTC, after 5 min."
    assert discussion["closed"] is False

    env.notifier.send(event("resolved"), delivery_id="n-3")

    assert discussion["comments"][-1].startswith("✅ **Run failed resolved**")
    assert github.by_id("D_1")["closed"] is True
    # A retried resolve of the first finds its comment and leaves the closed discussion alone.
    env.notifier.send({**QUALITY, "event": "resolved"}, delivery_id="q-3")
    assert len(discussion["comments"]) == 3 and len(github.of("close")) == 1


@pytest.mark.parametrize("change", ["another-run", "closed", "foreign"])
def test_only_an_open_discussion_the_app_wrote_for_the_same_run_is_joined(
    github: FakeGitHub, make: Callable[..., Env], change: str
) -> None:
    env = make()
    if change == "foreign":
        github.discussions.append(
            {
                "id": "D_x",
                "title": "t",
                "body": run_marker("dev", "run-9"),
                "closed": False,
                "comments": [],
                "foreign": True,
            }
        )
    else:
        env.notifier.send(OPENED, delivery_id="n-1")
        if change == "closed":
            github.discussions[0]["closed"] = True

    quality = {**QUALITY, "run_id": "run-10"} if change == "another-run" else QUALITY
    result = env.notifier.send(quality, delivery_id="q-1")

    assert result.url == url_of(github.discussions[-1]["id"])
    assert github.discussions[-1]["title"].startswith("[critical] quality")
    assert all(not d["comments"] for d in github.discussions)


def test_the_installation_token_is_scoped_to_the_one_repository_and_signed_as_the_app(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    make().notifier.send(OPENED, delivery_id="n-1")

    (lookup,) = github.of("installation")
    assert lookup["url"] == "https://api.github.com/repos/acme/ops-alerts/installation"
    (grant,) = github.of("token")
    assert grant["url"] == "https://api.github.com/app/installations/42/access_tokens"
    assert grant["json"] == {
        "repositories": ["ops-alerts"],
        "permissions": {"discussions": "write", "metadata": "read"},
    }
    for call in (lookup, grant):
        claims = jwt.decode(
            call["headers"]["Authorization"].removeprefix("Bearer "),
            PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_iat": False, "verify_exp": False},
        )
        assert claims["iss"] == "12345"
        assert claims["iat"] == int(START) - 60
        assert 0 < claims["exp"] - START <= 600
    graphql = [call for call in github.calls if call["route"] not in ("installation", "token")]
    assert graphql and all(call["headers"]["Authorization"] == f"Bearer {TOKEN}" for call in graphql)


def test_the_token_is_reused_until_five_minutes_before_it_expires(github: FakeGitHub, make: Callable[..., Env]) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")
    github.now = START + 3600 - 301
    env.notifier.send(event("escalated"), delivery_id="n-2")
    assert len(github.of("token")) == 1

    github.now = START + 3600 - 299
    env.notifier.send(event("escalated"), delivery_id="n-3")

    assert len(github.of("token")) == 2
    assert len(github.of("installation")) == 1


def test_the_category_matches_by_name_without_case_or_by_slug(github: FakeGitHub, make: Callable[..., Env]) -> None:
    github.category = "On Call"
    for category in ("on call", "ON CALL"):
        incident = {**OPENED, "incident_id": f"inc-{category}", "run_id": f"run-{category}"}
        assert make(category=category).notifier.send(incident, delivery_id=f"n-{category}").status == "delivered"
    other = {**OPENED, "incident_id": "inc-slug", "run_id": "run-slug"}
    assert make(category="general").notifier.send(other, delivery_id="n-slug").status == "delivered"
    assert [d["category"] for d in github.discussions] == ["C_alerts", "C_alerts", "C_other"]


def test_the_lookup_reads_the_newest_fifty_by_creation_time(github: FakeGitHub, make: Callable[..., Env]) -> None:
    make().notifier.send(OPENED, delivery_id="n-1")

    query = github.of("recent")[0]["json"]["query"]
    assert "first: 50" in query
    assert "orderBy: {field: CREATED_AT, direction: DESC}" in query
    assert "comments(last: 20)" in query


def test_a_ghes_api_url_gets_its_own_graphql_endpoint(github: FakeGitHub, make: Callable[..., Env]) -> None:
    assert graphql_url("https://api.github.com") == "https://api.github.com/graphql"
    assert graphql_url("https://ghe.example.com/api/v3/") == "https://ghe.example.com/api/graphql"

    make(api_url="https://ghe.example.com/api/v3").notifier.send(OPENED, delivery_id="n-1")

    assert github.of("installation")[0]["url"] == "https://ghe.example.com/api/v3/repos/acme/ops-alerts/installation"
    assert {c["url"] for c in github.of("create")} == {"https://ghe.example.com/api/graphql"}


def test_requests_are_not_redirected_and_use_the_given_ca_file(github: FakeGitHub, make: Callable[..., Env]) -> None:
    make(ca_file="/etc/ca.pem").notifier.send(OPENED, delivery_id="n-1")

    assert all(call["kwargs"]["allow_redirects"] is False for call in github.calls)
    assert all(call["kwargs"]["verify"] == "/etc/ca.pem" for call in github.calls)


@pytest.mark.parametrize(
    ("message", "kwargs"),
    [
        ("https", {"api_url": "http://api.github.com"}),
        ("owner/name", {"repository": "acme"}),
        ("owner/name", {"repository": "acme/a/b"}),
        ("category", {"category": " "}),
    ],
)
def test_a_receiver_that_cannot_work_is_refused_at_construction(message: str, kwargs: dict[str, Any]) -> None:
    settings: dict[str, Any] = {
        "repository": "acme/ops",
        "category": "Alerts",
        "app_id": "1",
        "private_key": PRIVATE_PEM,
    }
    settings.update(kwargs)

    with pytest.raises(ValueError, match=message):
        GitHubDiscussionsNotifier("team", **settings)


DISABLED = {
    "data": {"repository": {"id": "R_1", "hasDiscussionsEnabled": False, "discussionCategories": {"nodes": []}}}
}


@pytest.mark.parametrize(
    ("route", "fault", "status", "cause"),
    [
        pytest.param(
            "installation",
            reply(404, {"message": "Not Found"}),
            404,
            "not installed on acme/ops-alerts",
            id="app-not-installed",
        ),
        pytest.param(
            "token", reply(401, {"message": "Bad credentials"}), 401, "app id and private key", id="bad-credentials"
        ),
        pytest.param("token", reply(422, {"message": "x"}), 422, "cannot be granted", id="permission-not-grantable"),
        pytest.param("repository", reply(200, DISABLED), 200, "Discussions are not enabled", id="discussions-disabled"),
        pytest.param(
            "repository", reply(200, {"data": {"repository": None}}), 200, "was not found", id="repository-not-visible"
        ),
        pytest.param(
            "repository", graphql_errors("FORBIDDEN"), 200, "lacks discussions: write", id="graphql-forbidden"
        ),
        pytest.param("repository", graphql_errors("NOT_FOUND"), 200, "could not find", id="graphql-not-found"),
        pytest.param("repository", graphql_errors("WEIRD"), 200, "errors of type WEIRD", id="graphql-other"),
        pytest.param(
            "create", graphql_errors("FORBIDDEN", status=403), 403, "lacks discussions: write", id="forbidden-non-200"
        ),
        pytest.param(
            "create",
            reply(403, {"message": "Resource not accessible"}),
            403,
            "lacks discussions: write",
            id="plain-403",
        ),
        pytest.param("create", reply(422, {"message": "x"}), 422, "HTTP 422", id="other-4xx"),
    ],
)
def test_what_will_not_change_on_retry_is_rejected_once_with_an_operator_cause(
    github: FakeGitHub,
    make: Callable[..., Env],
    caplog: pytest.LogCaptureFixture,
    route: str,
    fault: requests.Response,
    status: int,
    cause: str,
) -> None:
    github.faults[route] = [fault]
    env = make()
    caplog.set_level(logging.INFO)

    result = env.notifier.send(OPENED, delivery_id="n-1")

    assert (result.status, result.http_status, result.attempts) == ("rejected", status, 1)
    assert env.sleeps == []
    rejections = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
    assert len(rejections) == 1
    assert "team" in rejections[0] and cause in rejections[0]
    assert github.created == 0


def test_a_category_the_repository_lacks_is_rejected_naming_it(
    github: FakeGitHub, make: Callable[..., Env], caplog: pytest.LogCaptureFixture
) -> None:
    github.category = "Other"
    caplog.set_level(logging.INFO)

    result = make().notifier.send(OPENED, delivery_id="n-1")

    assert (result.status, result.attempts) == ("rejected", 1)
    assert "category 'alerts' not found in acme/ops-alerts" in caplog.text
    assert github.created == 0


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(reply(500), id="500"),
        pytest.param(reply(503), id="503"),
        pytest.param(reply(408), id="408"),
        pytest.param(reply(429), id="429"),
        pytest.param(reply(403, {"message": "You have exceeded a secondary rate limit."}), id="secondary-limit-403"),
        pytest.param(reply(403, {"message": "Forbidden"}, {"Retry-After": "1"}), id="retry-after-403"),
        pytest.param(reply(403, {"message": "Forbidden"}, {"X-RateLimit-Remaining": "0"}), id="primary-limit-403"),
        pytest.param(graphql_errors("RATE_LIMITED"), id="graphql-rate-limited"),
        pytest.param(graphql_errors("RATE_LIMITED", status=403), id="graphql-rate-limited-non-200"),
        pytest.param(requests.ConnectionError("https://api.github.com refused"), id="connection"),
        pytest.param(requests.Timeout("slow"), id="timeout"),
        pytest.param(reply(200, {"data": None}), id="no-data"),
    ],
)
def test_transient_failures_are_retried_and_exhaustion_is_retryable(
    github: FakeGitHub, make: Callable[..., Env], fault: requests.Response | Exception
) -> None:
    github.faults["repository"] = [fault, fault, fault]
    env = make()

    exhausted = env.notifier.send(OPENED, delivery_id="n-1")

    assert (exhausted.status, exhausted.attempts) == ("retryable", 3)
    assert len(env.sleeps) == 2
    assert github.discussions == []

    github.faults["repository"] = [fault]
    recovered = env.notifier.send(OPENED, delivery_id="n-1")

    assert (recovered.status, recovered.attempts) == ("delivered", 2)
    assert len(github.discussions) == 1


def test_a_failed_5xx_reports_its_status_and_the_wait_honours_retry_after(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    github.faults["repository"] = [reply(503, headers={"Retry-After": "3"})] * 3
    env = make()

    result = env.notifier.send(OPENED, delivery_id="n-1")

    assert (result.status, result.http_status) == ("retryable", 503)
    assert env.sleeps == [3.0, 3.0]


def test_a_5xx_then_success_delivers_on_the_second_attempt(github: FakeGitHub, make: Callable[..., Env]) -> None:
    github.faults["create"] = [reply(502)]
    env = make()

    result = env.notifier.send(OPENED, delivery_id="n-1")

    assert (result.status, result.attempts) == ("delivered", 2)
    assert len(github.discussions) == 1


def test_an_unusable_private_key_is_rejected_without_quoting_it(
    github: FakeGitHub, make: Callable[..., Env], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env = make(private_key="-----BEGIN PRIVATE KEY-----\nSECRETKEYMATERIAL\n-----END PRIVATE KEY-----")

    result = env.notifier.send(OPENED, delivery_id="n-1")

    assert result.status == "rejected"
    assert github.calls == []
    assert "SECRETKEYMATERIAL" not in caplog.text
    assert "private key" in caplog.text


@pytest.mark.parametrize("scenario", ["delivered", "rejected", "retryable"])
def test_neither_the_key_nor_a_jwt_nor_the_token_reaches_logs_or_repr(
    github: FakeGitHub, make: Callable[..., Env], caplog: pytest.LogCaptureFixture, scenario: str
) -> None:
    caplog.set_level(logging.DEBUG)
    if scenario == "rejected":
        github.faults["create"] = [graphql_errors("FORBIDDEN")]
    if scenario == "retryable":
        github.faults["create"] = [reply(502)] * 3
    env = make()

    result = env.notifier.send(OPENED, delivery_id="n-1")

    assert result.status == scenario
    secrets = [TOKEN, PRIVATE_PEM, PRIVATE_PEM.splitlines()[1]]
    secrets += [call["headers"]["Authorization"].removeprefix("Bearer ") for call in github.of("installation")]
    seen = caplog.text + repr(env.notifier) + repr(result)
    for secret in secrets:
        assert secret and secret not in seen
    assert "internal detail" not in seen
    assert "order_revenue failed" not in seen


def test_a_null_category_node_is_skipped(github: FakeGitHub, make: Callable[..., Env]) -> None:
    matching = {"id": "C_alerts", "name": "Alerts", "slug": "alerts"}
    repository = {
        "id": "R_1",
        "hasDiscussionsEnabled": True,
        "discussionCategories": {"nodes": [None, matching]},
    }
    github.faults["repository"] = [reply(200, {"data": {"repository": repository}})]

    result = make().notifier.send(OPENED, delivery_id="n-1")

    assert (result.status, result.attempts) == ("delivered", 1)
    assert len(github.discussions) == 1


def test_a_comment_by_someone_else_carrying_the_delivery_marker_does_not_suppress_the_post(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    env = make()
    env.notifier.send(OPENED, delivery_id="n-1")
    github.discussions[0]["foreign_comments"] = [delivery_marker("n-3")]

    result = env.notifier.send(event("resolved"), delivery_id="n-3")

    assert result.status == "delivered"
    (discussion,) = github.discussions
    assert len(discussion["comments"]) == 1 and delivery_marker("n-3") in discussion["comments"][0]
    assert discussion["closed"] is True


def test_a_discussion_by_someone_else_carrying_the_delivery_marker_does_not_suppress_the_post(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    github.discussions.append(
        {"id": "D_x", "title": "t", "body": delivery_marker("n-1"), "closed": False, "comments": [], "foreign": True}
    )

    make().notifier.send(OPENED, delivery_id="n-1")

    assert github.created == 1


def test_a_discussion_by_someone_else_carrying_the_incident_marker_is_not_commented_on(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    github.discussions.append(
        {"id": "D_x", "title": "t", "body": incident_marker("inc-1"), "closed": False, "comments": [], "foreign": True}
    )

    result = make().notifier.send(event("escalated"), delivery_id="n-2")

    assert result.status == "delivered"
    assert github.by_id("D_x")["comments"] == []
    assert github.created == 1
    assert delivery_marker("n-2") in github.discussions[-1]["body"]


def test_a_late_opened_comments_on_the_discussion_a_resolved_already_created_and_closed(
    github: FakeGitHub, make: Callable[..., Env]
) -> None:
    env = make()
    env.notifier.send(event("resolved"), delivery_id="n-3")
    (fallback,) = github.discussions
    assert fallback["closed"] is True

    result = env.notifier.send(OPENED, delivery_id="n-1")

    assert result.status == "delivered"
    assert github.created == 1 and len(github.discussions) == 1
    assert len(fallback["comments"]) == 1 and delivery_marker("n-1") in fallback["comments"][0]
    assert fallback["closed"] is True
    assert len(github.of("close")) == 1


class _SlowSession:
    """Every request takes `seconds` on the fake clock and is answered with 503."""

    def __init__(self, github: FakeGitHub, seconds: float) -> None:
        self.github = github
        self.seconds = seconds
        self.started: list[float] = []

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._slow()

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._slow()

    def _slow(self) -> requests.Response:
        self.started.append(self.github.now)
        self.github.now += self.seconds
        return reply(503)


def test_a_slow_delivery_stops_inside_the_time_budget(github: FakeGitHub, make: Callable[..., Env]) -> None:
    slow = _SlowSession(github, 30.0)
    start = github.now

    result = make(session=slow).notifier.send(OPENED, delivery_id="n-1")

    assert result.status == "retryable"
    assert result.attempts < 3
    assert github.now - start <= MAX_TOTAL_SECONDS
    assert all(started - start <= MAX_TOTAL_SECONDS - 15.0 for started in slow.started)


def test_a_spent_budget_issues_no_request(github: FakeGitHub, make: Callable[..., Env]) -> None:
    slow = _SlowSession(github, MAX_TOTAL_SECONDS)

    result = make(session=slow).notifier.send(OPENED, delivery_id="n-1")

    assert result.status == "retryable"
    assert len(slow.started) == 1
