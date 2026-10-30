"""Added latency of the perimeter (#176) on an interactive dashboard load.

The same dashboard load (the dashboard, then the data of its charts) is timed
through Traefik + forwardAuth + oauth2-proxy with a signed-in session cookie,
and straight at the Superset Service. The difference is the perimeter's cost.
"""

from __future__ import annotations

import os
import secrets
import statistics
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import Any

import requests

from olf.common import config, log
from olf.infra import access, superset
from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e._identity_github import (
    REQUEST_TIMEOUT,
    dex_points_at_stub,
    github_stub,
    sign_in_with_stub,
    stub_host,
)
from olf.infra.e2e._perimeter import PORTAL_REF
from olf.infra.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise

WARMUP = 3
SAMPLES = 20
CHARTS_PER_LOAD = 3
# ponytail: generous bounds - one kind node on a shared CI runner is noisy, and
# a forwardAuth hop is a loopback call to oauth2-proxy (a cookie decrypt, no
# issuer round trip), so a healthy perimeter adds a few ms. These catch a
# regression to a per-request issuer call or a sync token refresh, not jitter.
# Only the median gates: with 20 samples the p95 is the single slowest load,
# which a shared runner can spike on its own, so it is reported, not enforced.
MAX_ADDED_MEDIAN_MS = 100.0


def summarize(seconds: Sequence[float]) -> tuple[float, float]:
    """Median and p95 in milliseconds."""
    ms = [s * 1000 for s in seconds]
    return statistics.median(ms), statistics.quantiles(ms, n=20)[18]


def require_acceptable(direct: tuple[float, float], via: tuple[float, float]) -> tuple[float, float]:
    added_median, added_p95 = via[0] - direct[0], via[1] - direct[1]
    if added_median > MAX_ADDED_MEDIAN_MS:
        raise E2EError(
            f"the perimeter adds {added_median:.0f} ms (median) to a dashboard load, "
            f"over the {MAX_ADDED_MEDIAN_MS:.0f} ms bound (p95 {added_p95:.0f} ms, reported only)"
        )
    return added_median, added_p95


def _timed_loads(load: Callable[[], None]) -> list[float]:
    for _ in range(WARMUP):
        load()
    samples = []
    for _ in range(SAMPLES):
        start = time.perf_counter()
        load()
        samples.append(time.perf_counter() - start)
    return samples


def _dashboard_load(
    base: str,
    slug: str,
    datasets: Sequence[Mapping[str, Any]],
    headers: Mapping[str, str],
    cookies: MutableMapping[str, str],
) -> Callable[[], None]:
    session = requests.Session()  # keep-alive, as a browser has
    session.cookies.update(cookies)
    csrf = session.get(f"{base}/api/v1/security/csrf_token/", headers=headers, timeout=REQUEST_TIMEOUT)
    csrf.raise_for_status()
    post_headers = {**headers, "X-CSRFToken": csrf.json()["result"], "Referer": base}

    def load() -> None:
        replies = [session.get(f"{base}/api/v1/dashboard/{slug}", headers=headers, timeout=REQUEST_TIMEOUT)]
        for dataset in datasets:
            # Imported charts carry no saved query context, so each is queried as a count over its dataset.
            body = {
                "datasource": dataset,
                "queries": [{"metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "count"}]}],
                "result_format": "json",
                "result_type": "full",
            }
            replies.append(
                session.post(f"{base}/api/v1/chart/data", json=body, headers=post_headers, timeout=REQUEST_TIMEOUT)
            )
        for reply in replies:
            if reply.status_code >= 400:
                raise E2EError(f"{reply.url}: HTTP {reply.status_code} {reply.text[:200]} while measuring latency")

    return load


def check_perimeter_latency(cfg: E2EConfig) -> None:
    # Skip on the same conditions as the perimeter checks, which it builds on.
    contracts = load_provider_contracts_or_raise(cfg)
    access_contract, identity = contracts["shared"]["access"], contracts["shared"]["identity"]
    perimeter = access_contract.get("perimeter")
    if not perimeter or not perimeter["enabled"] or identity.get("adapter") != "dex":
        log.info("Skipping the perimeter latency measurement: it needs an enabled perimeter and the Dex adapter.")
        return
    if config.truthy(os.environ.get(access.PORT_FORWARD_ENV, "")) or not dex_points_at_stub(cfg):
        log.info("Skipping the perimeter latency measurement: it needs the ingress and the GitHub stub.")
        return
    ref = f"stage/{config.env(CONTRACT_STAGE_ENV, 'dev')}/reporting"
    routes = {r: route["url"] for r, route in access_contract["routes"].items()}
    # The most privileged mapped role, so the grant on Superset is never the variable.
    role = next(r for r in identity["roles"]["precedence"] if r in identity["role_mapping"])
    login = f"olf-e2e-latency-{secrets.token_hex(3)}"
    log.step("Measuring the perimeter's added dashboard latency...")
    with (
        access.service_url(
            "shared/identity",
            service="dex",
            remote_port=5556,
            namespace=cfg.platform_namespace,
            log_path=f"/tmp/openlakeforge-{cfg.env}-dex-port-forward.log",
            shared_namespace=cfg.platform_namespace,
            kube_context=cfg.kube_context,
        ),
        github_stub(cfg, {login: identity["role_mapping"][role]}),
        access.service_url(
            ref,
            service="superset",
            remote_port=8088,
            namespace=cfg.namespace,
            local_port=cfg.superset_local_port,
            log_path=f"/tmp/openlakeforge-{cfg.env}-superset-latency-port-forward.log",
            shared_namespace=cfg.platform_namespace,
            kube_context=cfg.kube_context,
        ) as direct_url,
    ):
        browser, _ = sign_in_with_stub(routes[PORTAL_REF], login, stub_host(cfg))
        cookies = {c.name: c.value or "" for c in browser.cookies}
        # Superset's own API login is the same on both paths; only the route differs.
        password = superset.admin_password_for(cfg.namespace, kube_context=cfg.kube_context)
        auth = requests.post(
            f"{direct_url}/api/v1/security/login",
            json={"username": "admin", "password": password, "provider": "db", "refresh": True},
            timeout=REQUEST_TIMEOUT,
        )
        auth.raise_for_status()
        headers = {"Accept": "application/json", "Authorization": f"Bearer {auth.json()['access_token']}"}
        dashboards = requests.get(f"{direct_url}/api/v1/dashboard/", headers=headers, timeout=REQUEST_TIMEOUT).json()
        slug = str(dashboards["result"][0]["id"])
        charts = requests.get(f"{direct_url}/api/v1/dashboard/{slug}/charts", headers=headers, timeout=REQUEST_TIMEOUT)
        chart_ids = [c["id"] for c in charts.json()["result"]][:CHARTS_PER_LOAD]
        datasets = []
        for chart_id in chart_ids:
            chart = requests.get(f"{direct_url}/api/v1/chart/{chart_id}", headers=headers, timeout=REQUEST_TIMEOUT)
            result = chart.json()["result"]
            datasets.append({"id": result["datasource_id"], "type": result["datasource_type"]})
        direct = summarize(_timed_loads(_dashboard_load(direct_url, slug, datasets, headers, {})))
        via = summarize(_timed_loads(_dashboard_load(routes[ref], slug, datasets, headers, cookies)))
    added = require_acceptable(direct, via)
    log.info(
        f"Dashboard load ({len(chart_ids)} charts, {SAMPLES} samples): direct {direct[0]:.0f}/{direct[1]:.0f} ms, "
        f"through the perimeter {via[0]:.0f}/{via[1]:.0f} ms (median/p95); "
        f"perimeter adds {added[0]:.0f}/{added[1]:.0f} ms"
    )
