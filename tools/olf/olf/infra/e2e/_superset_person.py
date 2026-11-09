"""A person's dashboard data through Superset (ADR 0019 section 9, #987).

The other Superset checks use the local `admin`, which holds no Dex token and
cannot query Trino by design. This one signs in as a person through the
perimeter and Superset's own sign-in, then asks for a chart's data: the rows
only come back if the sign-in token reached Trino and Polaris granted it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import requests

from olf.common import log
from olf.infra.e2e._identity_github import REQUEST_TIMEOUT
from olf.infra.e2e._shell import E2EError


def reader_role(identity: Mapping[str, Any], stage: str) -> str | None:
    """The least privileged mapped role that may open the stage's dashboards and read its Gold."""
    roles = identity["roles"]
    for role in reversed(roles["precedence"]):
        if (
            role in identity["role_mapping"]
            and role in roles["grants"].get("reporting", {})
            and roles.get("data_access", {}).get(role, {}).get(stage, {}).get("gold")
        ):
            return str(role)
    return None


def refused_role(identity: Mapping[str, Any], stage: str) -> str | None:
    """A mapped role that may open the stage's dashboards but whose Polaris grants read none of its Gold."""
    roles = identity["roles"]
    for role in roles["precedence"]:
        if (
            role in identity["role_mapping"]
            and role in roles["grants"].get("reporting", {})
            and not roles.get("data_access", {}).get(role, {}).get(stage, {}).get("gold")
        ):
            return str(role)
    return None


def _chart_data(
    base: str,
    role: str,
    stage: str,
    login: str,
    sign_in: Callable[[str, str], tuple[requests.Session, requests.Response]],
) -> tuple[requests.Response, Mapping[str, Any]]:
    """Sign `role` in to the stage's Superset and ask for the first dashboard's first dataset's row count."""
    browser, _ = sign_in(f"{base}/login/", login)
    headers = {"Accept": "application/json"}
    dashboards = browser.get(f"{base}/api/v1/dashboard/", headers=headers, timeout=REQUEST_TIMEOUT)
    if dashboards.status_code != 200 or not dashboards.json().get("result"):
        raise E2EError(f"{role} signed in to Superset on {stage} but lists no dashboard: HTTP {dashboards.status_code}")
    dashboard = dashboards.json()["result"][0]["id"]
    datasets = browser.get(f"{base}/api/v1/dashboard/{dashboard}/datasets", headers=headers, timeout=REQUEST_TIMEOUT)
    datasets.raise_for_status()
    dataset = datasets.json()["result"][0]
    body = {
        "datasource": {"id": dataset["id"], "type": dataset.get("type") or "table"},
        "queries": [{"metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "count"}]}],
        "result_format": "json",
        "result_type": "full",
    }
    reply = browser.post(f"{base}/api/v1/chart/data", json=body, headers=headers, timeout=REQUEST_TIMEOUT * 4)
    return reply, dataset


def _rows(reply: requests.Response) -> list[Any] | None:
    return reply.json().get("result", [{}])[0].get("data") if reply.status_code == 200 else None


def check_person_chart_data(
    identity: Mapping[str, Any],
    routes: Mapping[str, str],
    stage: str,
    logins: Mapping[str | None, str],
    sign_in: Callable[[str, str], tuple[requests.Session, requests.Response]],
) -> None:
    ref = f"stage/{stage}/reporting"
    role = reader_role(identity, stage)
    if ref not in routes or role is None or role not in logins:
        log.info(f"Skipping the per-person chart check on {stage}: no reporting route or no role reads its Gold.")
        return
    base = routes[ref].rstrip("/")
    log.step(f"Checking {role} gets a chart's rows from Superset on {stage}...")
    reply, dataset = _chart_data(base, role, stage, logins[role], sign_in)
    rows = _rows(reply)
    if not rows or not rows[0].get("count"):
        # OAUTH2_REDIRECT here means the sign-in token was not kept for the database.
        raise E2EError(
            f"{role} on {stage}: the data of dataset {dataset.get('table_name')} returned no rows, "
            f"HTTP {reply.status_code}: {reply.text[:300]}"
        )
    log.info(f"{role} on {stage} read {rows[0]['count']} rows of {dataset.get('table_name')} through Superset.")
    denied = refused_role(identity, stage)
    if denied is None or denied not in logins:
        log.info(f"Skipping the refused-reader chart check on {stage}: every mapped role reads its Gold.")
        return
    # Right after the reader's request, so a result cached across users would answer it (#970).
    log.step(f"Checking {denied} gets no rows of the same chart on {stage}, after {role} read it...")
    reply, _ = _chart_data(base, denied, stage, logins[denied], sign_in)
    if _rows(reply):
        raise E2EError(f"isolation breach: {denied} read {dataset.get('table_name')} on {stage} through Superset")
    if "OAUTH2" in reply.text:
        # The refusal must be Polaris's, not a sign-in token Superset failed to keep.
        raise E2EError(f"{denied} on {stage} was asked to sign in to the database again: {reply.text[:300]}")
