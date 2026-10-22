"""Issuer-outage recovery drill (#177): Dex is down, break-glass still reaches the services.

Dex is scaled to zero, a fresh perimeter sign-in must fail, Trino and Dagster
must answer through recorded break-glass sessions, and after Dex is restored a
fresh sign-in must work again. Dex is always restored, even when a step fails.
The two timings it logs are the ones recorded in
docs/architecture/identity-sessions-and-recovery.md.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import requests

from olf import break_glass, log
from olf.deployment.engine import Toolkit
from olf.e2e._identity_github import dex_points_at_stub, github_stub, sign_in_with_stub, stub_host
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

REASON = "e2e issuer-outage drill"
PORTS = {"trino": 15558, "dagster": 15557}
LOCAL_CA_CONFIGMAP = "olf-local-ca"
# Trino's certificate names `localhost`, not the address the forward listens on.
HOST = "localhost"
RESTORE_ATTEMPTS = 30


@dataclass(frozen=True)
class Timings:
    outage_to_break_glass_seconds: float
    restore_to_sign_in_seconds: float


def run_drill(
    *,
    scale_dex: Callable[[int], object],
    wait_dex: Callable[[bool], None],
    signed_in: Callable[[], bool],
    reach: Callable[[str], int | None],
    records: Callable[[], list[dict[str, str]]],
    replicas: int,
    clock: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = time.sleep,
) -> Timings:
    scale_dex(0)
    try:
        wait_dex(False)
        down = clock()
        if signed_in():
            raise E2EError("A fresh sign-in succeeded with Dex scaled to zero.")
        first_answer = 0.0
        for service in PORTS:
            status = reach(service)
            if status != 200:
                raise E2EError(f"{service} did not answer 200 through break-glass during the outage (got {status}).")
            first_answer = first_answer or clock() - down
        done = records()
        for service in PORTS:
            mine = [r for r in done if r.get("reason") == REASON and r.get("service") == service]
            if not any(r.get("user") and r.get("ended") and "duration_seconds" in r for r in mine):
                raise E2EError(f"No complete break-glass record for {service}: {done}")
    finally:
        scale_dex(replicas)
    wait_dex(True)
    restored = clock()
    for _attempt in range(RESTORE_ATTEMPTS):
        if signed_in():
            return Timings(first_answer, clock() - restored)
        pause(2)
    raise E2EError("Sign-in did not work again after Dex was restored.")


def check_issuer_outage(cfg: E2EConfig) -> None:
    contracts = load_provider_contracts_or_raise(cfg)
    identity = contracts["shared"]["identity"]
    if not (contracts["shared"]["access"].get("perimeter") or {}).get("enabled") or identity.get("adapter") != "dex":
        log.info("Skipping the issuer-outage drill: it needs the Dex adapter behind an enabled perimeter.")
        return
    if not dex_points_at_stub(cfg):
        log.info("Skipping the issuer-outage drill: Dex does not point at the GitHub stub.")
        return
    log.step("Running the issuer-outage recovery drill (Dex scaled to zero)...")
    namespace = cfg.platform_namespace
    portal = contracts["shared"]["access"]["routes"]["shared/portal"]["url"]
    login = "olf-e2e-outage"
    team = identity["role_mapping"][identity["roles"]["precedence"][0]]
    tls_port = break_glass.trino_tls_port(contracts, next(iter(contracts["stages"])))
    cluster = break_glass.Cluster(Toolkit.default().kubectl, cfg.kube_context, None, namespace, cfg.namespace)
    dex = ["-n", namespace, "deploy/dex"]
    replicas = int(kubectl(cfg, ["get", *dex, "-o", "jsonpath={.spec.replicas}"], capture=True) or 1)

    def wait_dex(up: bool) -> None:
        if up:
            kubectl(cfg, ["rollout", "status", *dex, "--timeout=180s"])
            return
        for _attempt in range(90):
            ready = kubectl(cfg, ["get", *dex, "-o", "jsonpath={.status.readyReplicas}"], capture=True)
            if not ready or int(ready) == 0:
                return
            time.sleep(2)
        raise E2EError("Dex was still Ready 180s after scaling it to zero.")

    def signed_in() -> bool:
        try:
            browser, landing = sign_in_with_stub(portal, login, stub_host(cfg))
        except requests.RequestException:
            return False
        session = [c for c in browser.cookies if c.name.startswith("_oauth2_proxy") and "csrf" not in c.name]
        return landing.status_code < 400 and bool(session)

    with tempfile.TemporaryDirectory() as scratch:
        ca = Path(scratch) / "ca.crt"

        def reach(service: str) -> int | None:
            https = service == "trino" and tls_port is not None
            if https and not ca.exists():
                args = ["get", "configmap", LOCAL_CA_CONFIGMAP, "-n", namespace, "-o", "jsonpath={.data.ca\\.crt}"]
                ca.write_text(kubectl(cfg, args, capture=True))
            path = "/v1/info" if service == "trino" else "/server_info"
            url = f"{'https' if https else 'http'}://{HOST}:{PORTS[service]}{path}"
            return probe(url, verify=str(ca) if https else True, service=service, cluster=cluster, tls_port=tls_port)

        def records() -> list[dict[str, str]]:
            return break_glass.recent(cluster.kubectl, namespace, limit=20, context=cfg.kube_context) or []

        with github_stub(cfg, {login: team}):
            timings = run_drill(
                scale_dex=lambda n: kubectl(cfg, ["scale", *dex, f"--replicas={n}"]),
                wait_dex=wait_dex,
                signed_in=signed_in,
                reach=reach,
                records=records,
                replicas=replicas,
            )
    log.info(
        f"Issuer-outage drill: first break-glass answer {timings.outage_to_break_glass_seconds:.0f}s after "
        f"Dex was down; sign-in worked {timings.restore_to_sign_in_seconds:.0f}s after Dex was Ready again."
    )


def probe(
    url: str, *, verify: str | bool, service: str, cluster: break_glass.Cluster, tls_port: int | None
) -> int | None:
    """Open a recorded break-glass session to `service` and GET `url` through it once it answers."""
    answered: list[int] = []

    def reach(argv: Sequence[str], **_: object) -> None:
        process = subprocess.Popen(argv)
        try:
            for _attempt in range(30):
                try:
                    answered.append(requests.get(url, verify=verify, timeout=3).status_code)
                    return
                except requests.RequestException:
                    time.sleep(1)
        finally:
            process.terminate()
            process.wait(timeout=10)

    break_glass.open_session(cluster, service, REASON, 1, local_port=PORTS[service], tls_port=tls_port, run=reach)
    return answered[0] if answered else None
