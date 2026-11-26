"""Alert delivery to GitHub Discussions, authenticated as the organization's GitHub App.

One discussion per incident: `opened` creates it, `escalated` and `resolved` comment on it, `resolved` also closes it.
Incidents opened by the same run share one discussion: a blocking quality check that fails a run opens `quality` and
`run_failed` together, and the second joins the first's discussion as a comment. A shared discussion closes once every
incident in it has resolved.

The evaluator keeps no discussion ids, so an incident's discussion is found by a hidden marker in the post or comment
that opened it, a run's by a third, and a delivery already posted is recognised by a fourth. Delivery is at-least-once
(ADR 0023, decision 3) and a retry runs the whole flow again, so every step checks the markers before it writes.

A delivered send answers the discussion's URL, which the evaluator keeps for the Portal to link.

Known limit: the lookup reads the category's 50 newest discussions (and the last 20 comments of each). An incident
whose discussion has been pushed out of that window gets a fresh discussion on its next event, and a delivery
retried after that long is posted again.

Markers are trusted only in content the App itself authored (`viewerDidAuthor`): in a public repository anyone can
comment, and a copied marker must neither suppress a delivery nor redirect an incident's comments.

The App's token is requested for the one repository with `discussions: write` and `metadata: read` only. Nothing here
logs or raises the key, a JWT, the token or a payload: rejections name the receiver and an operator-facing cause.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import jwt
import requests
from openlakeforge_services.notify import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_TOTAL_SECONDS,
    READ_TIMEOUT_SECONDS,
    DeliveryResult,
    _random_jitter,
    _retry_after,
    backoff_wait,
)

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"
NEWEST_DISCUSSIONS = 50
RECENT_COMMENTS = 20
# GitHub caps an App JWT at ten minutes; one minute of back-dating absorbs clock skew.
_JWT_BACKDATE_SECONDS = 60
_JWT_LIFETIME_SECONDS = 540
# A token lasts an hour. It is replaced this long before it expires so a send never starts with a token about to lapse.
_TOKEN_REFRESH_MARGIN_SECONDS = 300
_TOKEN_FALLBACK_SECONDS = 1800
_TOKEN_PERMISSIONS = {"discussions": "write", "metadata": "read"}
_API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

_CREATE_DISCUSSION = """
mutation($repository: ID!, $category: ID!, $title: String!, $body: String!) {
  createDiscussion(input: {repositoryId: $repository, categoryId: $category, title: $title, body: $body}) {
    discussion { id url }
  }
}"""
_ADD_COMMENT = """
mutation($discussion: ID!, $body: String!) {
  addDiscussionComment(input: {discussionId: $discussion, body: $body}) { comment { id } }
}"""
_CLOSE_DISCUSSION = """
mutation($discussion: ID!) {
  closeDiscussion(input: {discussionId: $discussion, reason: RESOLVED}) { discussion { id } }
}"""
_REPOSITORY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
    hasDiscussionsEnabled
    discussionCategories(first: 100) { nodes { id name slug } }
  }
}"""
# CREATED_AT is explicit because the default order is UPDATED_AT, which a comment on an old incident would reshuffle.
_RECENT = f"""
query($owner: String!, $name: String!, $category: ID!) {{
  repository(owner: $owner, name: $name) {{
    discussions(categoryId: $category, first: {NEWEST_DISCUSSIONS}, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      nodes {{
        id
        url
        body
        closed
        viewerDidAuthor
        comments(last: {RECENT_COMMENTS}) {{ nodes {{ body viewerDidAuthor }} }}
      }}
    }}
  }}
}}"""


class _Session(Protocol):
    """The slice of `requests.Session` this module uses; tests pass a fake."""

    def get(self, url: str, **kwargs: Any) -> requests.Response: ...

    def post(self, url: str, **kwargs: Any) -> requests.Response: ...


_REQUEST_SECONDS = CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS


class _Retryable(Exception):
    def __init__(self, cause: str, http_status: int | None = None, retry_after: float = 0.0) -> None:
        super().__init__(cause)
        self.http_status = http_status
        self.retry_after = retry_after


class _Rejected(Exception):
    def __init__(self, cause: str, http_status: int | None = None) -> None:
        super().__init__(cause)
        self.http_status = http_status


def incident_marker(incident_id: str) -> str:
    return f"<!-- olf-incident: {incident_id} -->"


def delivery_marker(delivery_id: str) -> str:
    return f"<!-- olf-delivery: {delivery_id} -->"


def run_marker(stage: str, run_id: str) -> str:
    return f"<!-- olf-run: {stage}:{run_id} -->"


def resolved_marker(incident_id: str) -> str:
    return f"<!-- olf-resolved: {incident_id} -->"


_INCIDENT_IDS = re.compile(r"<!-- olf-incident: (\S+) -->")
_RESOLVED_IDS = re.compile(r"<!-- olf-resolved: (\S+) -->")


def graphql_url(api_url: str) -> str:
    """`<api_url>/graphql`, or `<host>/api/graphql` for a GitHub Enterprise Server `<host>/api/v3`."""
    base = api_url.rstrip("/")
    if base.endswith("/api/v3"):
        return f"{base.removesuffix('/api/v3')}/api/graphql"
    return f"{base}/graphql"


class GitHubDiscussionsNotifier:
    def __init__(
        self,
        name: str,
        *,
        repository: str,
        category: str,
        app_id: str,
        private_key: str,
        api_url: str = DEFAULT_API_URL,
        ca_file: str | None = None,
        # Seams for tests; production uses a real session, clock, sleep and RNG.
        session: _Session | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = _random_jitter,
    ) -> None:
        owner, _, repo = repository.partition("/")
        if not owner or not repo or "/" in repo:
            raise ValueError(f"github-discussions receiver {name!r}: the repository must be owner/name")
        if urlsplit(api_url).scheme != "https":
            raise ValueError(f"github-discussions receiver {name!r}: the API URL must use https")
        if not category.strip():
            raise ValueError(f"github-discussions receiver {name!r}: the category must not be empty")
        self.name = name
        self._owner = owner
        self._repo = repo
        self._category = category.strip()
        self._app_id = app_id
        self._private_key = private_key
        self._api_url = api_url.rstrip("/")
        self._graphql_url = graphql_url(api_url)
        self._ca_file = ca_file
        self._session = session if session is not None else cast(_Session, requests.Session())
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._installation_id: int | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._repository_id: str | None = None
        self._category_id: str | None = None
        # The last HTTP status of the attempt in flight, reported when the failure itself carries none.
        self._last_status: int | None = None
        self._deadline = 0.0

    def __repr__(self) -> str:
        return f"GitHubDiscussionsNotifier(name={self.name!r})"

    def send(self, payload: Mapping[str, Any], *, delivery_id: str) -> DeliveryResult:
        status: int | None = None
        # One send makes several requests per attempt, so the bound is a deadline, not an attempt count: the run.py
        # delivery budget assumes a send ends within MAX_TOTAL_SECONDS, as the webhook notifier does.
        self._deadline = self._clock() + MAX_TOTAL_SECONDS
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after = 0.0
            self._last_status = None
            try:
                url = self._deliver(payload, delivery_id)
            except _Rejected as rejected:
                logger.warning("github-discussions %s: rejected: %s", self.name, rejected)
                return DeliveryResult("rejected", rejected.http_status or self._last_status, attempt)
            except _Retryable as failed:
                logger.warning("github-discussions %s: attempt %d failed: %s", self.name, attempt, failed)
                status = failed.http_status or self._last_status
                retry_after = failed.retry_after
            else:
                return DeliveryResult("delivered", self._last_status, attempt, url)
            if attempt < MAX_ATTEMPTS:
                wait = backoff_wait(attempt, retry_after, self._jitter)
                if wait + _REQUEST_SECONDS > self._deadline - self._clock():
                    logger.warning("github-discussions %s: the delivery time budget is spent", self.name)
                    return DeliveryResult("retryable", status, attempt)
                self._sleep(wait)
        return DeliveryResult("retryable", status, MAX_ATTEMPTS)

    def _deliver(self, payload: Mapping[str, Any], delivery_id: str) -> str | None:
        """Post the notification; the URL of the discussion it is in."""
        event = str(payload.get("event", ""))
        incident_id = str(payload.get("incident_id", ""))
        marker = delivery_marker(delivery_id)
        category_id = self._resolve_category()
        recent = self._recent_discussions(category_id)

        posted = next((d for d in recent if _carries(d, marker)), None)
        if posted is not None:
            # A retry after a partial send: the post is there, but the close that follows it may not have happened.
            if not posted.get("closed") and (event == "test" or (event == "resolved" and _all_resolved(posted))):
                self._close(str(posted["id"]))
            return _url(posted)

        markers = [marker]
        if incident_id:
            markers.insert(0, incident_marker(incident_id))
        run = _run_marker(payload)
        if run and event == "opened":
            markers.insert(0, run)

        if event in ("opened", "escalated", "resolved") and incident_id:
            existing = next((d for d in recent if _carries(d, incident_marker(incident_id))), None)
            joined = False
            if existing is None and event == "opened" and run is not None:
                existing = next((d for d in recent if not d.get("closed") and _carries(d, run)), None)
                joined = existing is not None
            if existing is not None:
                shared = joined or len(_incidents(existing)) > 1
                if event == "resolved":
                    markers.append(resolved_marker(incident_id))
                self._add_comment(str(existing["id"]), _comment(payload, event, markers, shared=shared))
                if event == "resolved" and not existing.get("closed") and _all_resolved(existing, incident_id):
                    self._close(str(existing["id"]))
                return _url(existing)

        if event == "resolved" and incident_id:
            markers.append(resolved_marker(incident_id))
        created, url = self._create_discussion(category_id, _title(payload, event), _body(payload, event, markers))
        if event in ("resolved", "test"):
            self._close(created)
        return url

    def _resolve_category(self) -> str:
        if self._category_id is not None:
            return self._category_id
        data = self._graphql(_REPOSITORY, {"owner": self._owner, "name": self._repo})
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise _Rejected(f"repository {self._owner}/{self._repo} was not found, or the GitHub App cannot see it")
        if not repository.get("hasDiscussionsEnabled"):
            raise _Rejected(f"Discussions are not enabled on {self._owner}/{self._repo}")
        wanted = self._category.casefold()
        for node in (repository.get("discussionCategories") or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            if wanted in (str(node.get("name", "")).casefold(), str(node.get("slug", "")).casefold()):
                self._repository_id = str(repository["id"])
                self._category_id = str(node["id"])
                return self._category_id
        raise _Rejected(f"category {self._category!r} not found in {self._owner}/{self._repo}")

    def _recent_discussions(self, category_id: str) -> list[dict[str, Any]]:
        data = self._graphql(_RECENT, {"owner": self._owner, "name": self._repo, "category": category_id})
        nodes = ((data.get("repository") or {}).get("discussions") or {}).get("nodes") or []
        return [node for node in nodes if isinstance(node, dict)]

    def _create_discussion(self, category_id: str, title: str, body: str) -> tuple[str, str | None]:
        data = self._graphql(
            _CREATE_DISCUSSION,
            {"repository": self._repository_id, "category": category_id, "title": title, "body": body},
        )
        try:
            discussion = data["createDiscussion"]["discussion"]
            return str(discussion["id"]), _url(discussion)
        except (KeyError, TypeError):
            raise _Retryable("GitHub answered createDiscussion without a discussion", self._last_status) from None

    def _add_comment(self, discussion_id: str, body: str) -> None:
        self._graphql(_ADD_COMMENT, {"discussion": discussion_id, "body": body})

    def _close(self, discussion_id: str) -> None:
        self._graphql(_CLOSE_DISCUSSION, {"discussion": discussion_id})

    def _graphql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "post",
            self._graphql_url,
            token=self._installation_token(),
            json_body={"query": query, "variables": dict(variables)},
        )
        data = _json(response).get("data")
        if not isinstance(data, dict):
            raise _Retryable("GitHub answered a GraphQL request without data", response.status_code)
        return data

    def _installation_token(self) -> str:
        if self._token is not None and self._clock() < self._token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        self._token = None
        app_token = self._app_jwt()
        repository = f"{self._owner}/{self._repo}"
        if self._installation_id is None:
            response = self._request(
                "get",
                f"{self._api_url}/repos/{repository}/installation",
                token=app_token,
                causes={404: f"the GitHub App is not installed on {repository}"},
            )
            installation = _json(response).get("id")
            if not isinstance(installation, int):
                raise _Retryable("GitHub answered the installation lookup without an id", response.status_code)
            self._installation_id = installation
        response = self._request(
            "post",
            f"{self._api_url}/app/installations/{self._installation_id}/access_tokens",
            token=app_token,
            json_body={"repositories": [self._repo], "permissions": _TOKEN_PERMISSIONS},
            causes={
                404: "the GitHub App installation no longer exists",
                422: f"the GitHub App cannot be granted discussions: write on {repository}",
            },
        )
        document = _json(response)
        token = document.get("token")
        if not isinstance(token, str) or not token:
            raise _Retryable("GitHub answered the token request without a token", response.status_code)
        self._token = token
        self._token_expires_at = _expiry(document.get("expires_at"), self._clock())
        return token

    def _app_jwt(self) -> str:
        now = int(self._clock())
        claims = {"iat": now - _JWT_BACKDATE_SECONDS, "exp": now + _JWT_LIFETIME_SECONDS, "iss": str(self._app_id)}
        try:
            return jwt.encode(claims, self._private_key, algorithm="RS256")
        except Exception:
            # The library's message can quote key material, so it is neither logged nor chained.
            raise _Rejected("the GitHub App private key is not a usable RSA key") from None

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        json_body: Mapping[str, Any] | None = None,
        causes: Mapping[int, str] | None = None,
    ) -> requests.Response:
        """One call, raising `_Retryable` or `_Rejected` for everything that is not a usable answer."""
        if self._deadline - self._clock() < _REQUEST_SECONDS:
            raise _Retryable("the delivery time budget is spent")
        send = self._session.post if method == "post" else self._session.get
        try:
            response = send(
                url,
                headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
                json=json_body,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=False,
                verify=self._ca_file or True,
            )
        except requests.RequestException:
            # The exception text can carry the URL and request details, so it is neither logged nor chained.
            raise _Retryable("could not reach the GitHub API") from None
        self._last_status = response.status_code
        self._classify(response, causes or {})
        return response

    def _classify(self, response: requests.Response, causes: Mapping[int, str]) -> None:
        status = response.status_code
        wait = _retry_after(response)
        if status in (408, 429) or status >= 500:
            raise _Retryable(f"GitHub answered HTTP {status}", status, wait)
        types = _error_types(response)
        # The primary limit answers 403 with no Retry-After and a plain message, only X-RateLimit-Remaining: 0.
        exhausted = response.headers.get("X-RateLimit-Remaining", "").strip() == "0"
        asked_to_wait = bool(response.headers.get("Retry-After")) or exhausted
        limited = status == 403 and (asked_to_wait or "rate limit" in _message(response))
        if limited or "RATE_LIMITED" in types:
            raise _Retryable("GitHub rate-limited the request", status, wait)
        if types:
            raise _Rejected(_graphql_cause(types, f"{self._owner}/{self._repo}"), status)
        if status == 401:
            # A cached token may have been revoked; the next send mints a new one.
            self._token = None
            raise _Rejected("GitHub rejected the App credentials (check the app id and private key)", status)
        if status >= 400:
            default = f"GitHub answered HTTP {status}"
            if status == 403:
                default = "the GitHub App lacks discussions: write on the repository"
            raise _Rejected(causes.get(status, default), status)


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_types(response: requests.Response) -> set[str]:
    errors = _json(response).get("errors")
    if not isinstance(errors, list) or not errors:
        return set()
    return {str(error.get("type") or "UNKNOWN") if isinstance(error, dict) else "UNKNOWN" for error in errors}


def _message(response: requests.Response) -> str:
    return str(_json(response).get("message", "")).lower()


def _graphql_cause(types: set[str], repository: str) -> str:
    if "FORBIDDEN" in types:
        return f"the GitHub App lacks discussions: write on {repository}"
    if "NOT_FOUND" in types:
        return f"GitHub could not find the discussion or repository on {repository}"
    return f"GitHub answered GraphQL errors of type {', '.join(sorted(types))}"


def _expiry(value: object, now: float) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return now + _TOKEN_FALLBACK_SECONDS


def _authored_texts(discussion: Mapping[str, Any]) -> list[str]:
    """The discussion's body and recent comments that the App itself wrote: the only places a marker is trusted."""
    texts = [str(discussion.get("body") or "")] if discussion.get("viewerDidAuthor") is True else []
    nodes = (discussion.get("comments") or {}).get("nodes") or []
    texts += [
        str(node.get("body") or "") for node in nodes if isinstance(node, dict) and node.get("viewerDidAuthor") is True
    ]
    return texts


def _carries(discussion: Mapping[str, Any], marker: str) -> bool:
    return any(marker in text for text in _authored_texts(discussion))


def _incidents(discussion: Mapping[str, Any]) -> set[str]:
    return {found for text in _authored_texts(discussion) for found in _INCIDENT_IDS.findall(text)}


def _all_resolved(discussion: Mapping[str, Any], resolving: str | None = None) -> bool:
    """Whether every incident in the discussion has resolved, counting `resolving`, whose comment is being posted."""
    resolved = {found for text in _authored_texts(discussion) for found in _RESOLVED_IDS.findall(text)}
    if resolving:
        resolved.add(resolving)
    return _incidents(discussion) <= resolved


def _url(discussion: Mapping[str, Any]) -> str | None:
    url = discussion.get("url")
    return url if isinstance(url, str) and url.startswith("https://") else None


def _run_marker(payload: Mapping[str, Any]) -> str | None:
    stage, run_id = payload.get("stage"), payload.get("run_id")
    return run_marker(str(stage), str(run_id)) if stage and run_id else None


_SEVERITY_MARK = {"critical": "🔴", "warning": "🟠"}
_LINK_LABELS = {"portal": "Open in the Portal", "run": "Dagster run", "runbook": "Runbook"}
_FIELDS = ("condition", "severity", "stage", "domain", "product", "table", "run_id", "opened_at", "event_at")
_TIMES = frozenset({"opened_at", "event_at"})


def _label(condition: object) -> str:
    return str(condition or "alert").replace("_", " ")


def _cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _moment(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _when(value: object) -> str:
    moment = _moment(value)
    return f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC" if moment else _cell(value)


def _clock(value: object) -> str:
    moment = _moment(value)
    return f"{moment.astimezone(UTC):%H:%M} UTC" if moment else _cell(value)


def _duration(start: object, end: object) -> str | None:
    began, ended = _moment(start), _moment(end)
    if began is None or ended is None or ended < began:
        return None
    minutes = int((ended - began).total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{hours // 24} d {hours % 24} h" if hours % 24 else f"{hours // 24} d"


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if not text or text[-1] in ".!?" else f"{text}."


def _where(payload: Mapping[str, Any]) -> str:
    """`**product** in **stage**`, or the stage alone for a platform condition."""
    stage = _cell(payload.get("stage") or "")
    scope = "/".join(_cell(part) for part in (payload.get("domain"), payload.get("product")) if part)
    if table := payload.get("table"):
        scope = f"{scope} · {_cell(table)}" if scope else _cell(table)
    if scope and stage:
        return f"**{scope}** in **{stage}**"
    return f"**{scope or stage or 'OpenLakeForge'}**"


def _links_line(payload: Mapping[str, Any]) -> str | None:
    links = payload.get("links")
    if not isinstance(links, Mapping) or not links:
        return None
    ordered = [name for name in _LINK_LABELS if name in links] + sorted(set(links) - set(_LINK_LABELS))
    return " · ".join(f"[{_LINK_LABELS.get(name, _cell(name))}]({_cell(links[name])})" for name in ordered)


def _details(payload: Mapping[str, Any]) -> list[str]:
    rows = [
        f"| {key.replace('_', ' ')} | {_when(payload[key]) if key in _TIMES else _cell(payload[key])} |"
        for key in _FIELDS
        if payload.get(key) not in (None, "")
    ]
    if not rows:
        return []
    return [
        "<details><summary>Details</summary>",
        "",
        "| Field | Value |",
        "| --- | --- |",
        *rows,
        "",
        "</details>",
        "",
    ]


def _title(payload: Mapping[str, Any], event: str) -> str:
    stage = payload.get("stage")
    if event == "test":
        return f"[test] OpenLakeForge alert test · {stage}" if stage else "[test] OpenLakeForge alert test"
    severity, condition = payload.get("severity"), payload.get("condition")
    scope = "/".join(str(part) for part in (payload.get("domain"), payload.get("product")) if part)
    head = " ".join(
        part for part in (f"[{severity}]" if severity else "", _label(condition) if condition else "") if part
    )
    title = " · ".join(str(part) for part in (head, stage, scope) if part) or "OpenLakeForge alert"
    # `opened` keeps the plain title; any other event got here because it is unknown or had no discussion to join.
    return title if event == "opened" else f"{event or 'alert'}: {title}"


def _lead(payload: Mapping[str, Any], event: str) -> list[str]:
    """The sentence a reader needs, then when, then where to look."""
    if event == "test":
        stage = _cell(payload.get("stage") or "")
        where = f" for stage **{stage}**" if stage else ""
        return [f"🧪 A test notification{where}, sent by `olf infra alerts test`. Nothing is wrong.", ""]
    severity = str(payload.get("severity") or "")
    mark = _SEVERITY_MARK.get(severity, "⚪")
    summary = _sentence(_cell(payload.get("summary") or _label(payload.get("condition"))))
    lines = [f"{mark} **{severity.capitalize() or 'Alert'}** · {_where(payload)}: {summary}", ""]
    when = [f"Opened {_when(payload['opened_at'])}"] if payload.get("opened_at") else []
    if run_id := payload.get("run_id"):
        when.append(f"run `{_cell(run_id)[:8]}`")
    if when:
        lines += [" · ".join(when), ""]
    if links := _links_line(payload):
        lines += [links, ""]
    return lines


def _body(payload: Mapping[str, Any], event: str, markers: list[str]) -> str:
    return "\n".join([*_lead(payload, event), *_details(payload), *markers])


def _comment(payload: Mapping[str, Any], event: str, markers: list[str], *, shared: bool) -> str:
    """A later event of the discussion's incident, or a second incident of the same run joining it."""
    label = _label(payload.get("condition"))
    severity = str(payload.get("severity") or "")
    at = payload.get("event_at")
    if event == "resolved":
        took = _duration(payload.get("opened_at"), at)
        head = f"{label.capitalize()} resolved" if shared else "Resolved"
        lines = [f"✅ **{head}** at {_clock(at)}" + (f", after {took}." if took else "."), ""]
    elif event == "escalated":
        what = f"{label.capitalize()} escalated" if shared else "Escalated"
        summary = _sentence(_cell(payload.get("summary") or ""))
        lines = [f"⬆️ **{what} to {severity}** at {_clock(at)}" + (f": {summary}" if summary else "."), ""]
    elif event == "opened" and shared:
        mark = _SEVERITY_MARK.get(severity, "⚪")
        summary = _sentence(_cell(payload.get("summary") or label))
        lines = [f"{mark} **{label.capitalize()}** ({severity}) on the same run: {summary}", ""]
        if links := _links_line(payload):
            lines += [links, ""]
        lines += _details(payload)
    else:
        lines = [*_lead(payload, event), *_details(payload)]
    return "\n".join([*lines, *markers])
