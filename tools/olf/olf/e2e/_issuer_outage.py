"""Issuer-outage recovery drill (#177): Dex is down, break-glass still reaches the services.

Dex is scaled to zero, a fresh perimeter sign-in must fail, Trino and Dagster
must answer through recorded break-glass sessions, and after Dex is restored a
fresh sign-in must work again. Dex is always restored, even when a step fails.
The two timings it logs are the ones recorded in
docs/architecture/identity/identity-sessions-and-recovery.md.
"""

from __future__ import annotations

import json
import os
import secrets
import select
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from olf import access, break_glass, config, log
from olf.e2e._identity_github import dex_points_at_stub, github_stub, sign_in_with_stub, stub_host
from olf.e2e._perimeter import check_unauthenticated_requests
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise
from olf.tooling.toolkit import Toolkit

REASON = "e2e issuer-outage drill"  # plus a per-run suffix: records are never deleted
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
    refused: Callable[[], bool],
    reach: Callable[[str], int | None],
    records: Callable[[], list[dict[str, str]]],
    ingresses: Callable[[], str],
    still_protected: Callable[[], None],
    replicas: int,
    reason: str = REASON,
    clock: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = time.sleep,
) -> Timings:
    before = ingresses()
    first_answer = 0.0
    ok = False
    try:
        scale_dex(0)
        wait_dex(False)
        down = clock()
        if not refused():
            raise E2EError("A fresh sign-in was not refused by the issuer's own error page with Dex scaled to zero.")
        for service in PORTS:
            status = reach(service)
            if status != 200:
                raise E2EError(f"{service} did not answer 200 through break-glass during the outage (got {status}).")
            first_answer = first_answer or clock() - down
        done = records()
        for service in PORTS:
            mine = [r for r in done if r.get("reason") == reason and r.get("service") == service]
            if not any(r.get("user") and r.get("ended") and "duration_seconds" in r for r in mine):
                raise E2EError(f"No complete break-glass record for {service}: {done}")
        ok = True
    finally:
        try:
            scale_dex(replicas)
            wait_dex(True)
        except E2EError as exc:
            if ok:
                raise
            log.info(f"Dex restore also failed: {exc}")  # the drill's own failure is the one to report
    if ingresses() != before:
        raise E2EError("The ingresses changed during the outage drill.")
    restored = clock()
    for _attempt in range(RESTORE_ATTEMPTS):
        if signed_in():
            elapsed = clock() - restored
            still_protected()
            return Timings(first_answer, elapsed)
        pause(2)
    raise E2EError("Sign-in did not work again after Dex was restored.")


def check_issuer_outage(cfg: E2EConfig) -> None:
    contracts = load_provider_contracts_or_raise(cfg)
    identity = contracts["shared"]["identity"]
    if not (contracts["shared"]["access"].get("perimeter") or {}).get("enabled") or identity.get("adapter") != "dex":
        log.info("Skipping the issuer-outage drill: it needs the Dex adapter behind an enabled perimeter.")
        return
    if config.truthy(os.environ.get(access.PORT_FORWARD_ENV, "")):
        log.info("Skipping the issuer-outage drill: OLF_PORT_FORWARD bypasses the ingress it signs in through.")
        return
    if not dex_points_at_stub(cfg):
        log.info("Skipping the issuer-outage drill: Dex does not point at the GitHub stub.")
        return
    log.step("Running the issuer-outage recovery drill (Dex scaled to zero)...")
    namespace = cfg.platform_namespace
    portal = contracts["shared"]["access"]["routes"]["shared/portal"]["url"]
    login = "olf-e2e-outage"
    team = next(identity["role_mapping"][r] for r in identity["roles"]["precedence"] if r in identity["role_mapping"])
    reason = f"{REASON} {secrets.token_hex(3)}"
    issuer_host = urlsplit(identity["issuer_url"]).hostname
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

    def ingresses() -> str:
        items = json.loads(kubectl(cfg, ["get", "ingress", "-A", "-o", "json"], capture=True))["items"]
        return json.dumps(
            sorted((i["metadata"]["namespace"], i["metadata"]["name"], i["spec"].get("rules")) for i in items),
            sort_keys=True,
        )

    def still_protected() -> None:
        access_contract = contracts["shared"]["access"]
        routes = {ref: route["url"] for ref, route in access_contract["routes"].items()}
        protected = access_contract["perimeter"]["protected_routes"]
        check_unauthenticated_requests(identity, protected, routes, login=f"{identity['issuer_url']}/auth")

    def walk() -> requests.Response:
        landing = sign_in_with_stub(portal, login, stub_host(cfg))[1]
        log.info(f"Drill sign-in ended at HTTP {landing.status_code} {landing.url}")
        return landing

    def refused() -> bool:
        # Only the issuer's own error page counts: a transport failure elsewhere proves nothing about Dex.
        landing = walk()
        return landing.status_code >= 400 and urlsplit(landing.url).hostname == issuer_host

    def signed_in() -> bool:
        # The walk ends on the portal once signed in. The session cookie is not read: it is scoped to
        # the parent domain and the jar does not keep it.
        try:
            landing = walk()
        except requests.exceptions.SSLError:
            raise  # a broken trust setup must not read as "Dex is not back yet"
        except requests.RequestException:
            return False
        return landing.status_code < 400 and urlsplit(landing.url).hostname == urlsplit(portal).hostname

    def logs_of_failure() -> None:
        for target in ("-l=openlakeforge.io/workload=oauth2-proxy", "deploy/dex"):
            args = ["logs", "-n", namespace, target, "--tail=20", "--prefix"]
            try:
                log.info(kubectl(cfg, args, capture=True))
            except E2EError as exc:
                log.info(str(exc))

    with tempfile.TemporaryDirectory() as scratch:
        ca = Path(scratch) / "ca.crt"

        def reach(service: str) -> int | None:
            https = service == "trino" and tls_port is not None
            if https and not ca.exists():
                args = ["get", "configmap", LOCAL_CA_CONFIGMAP, "-n", namespace, "-o", "jsonpath={.data.ca\\.crt}"]
                ca.write_text(kubectl(cfg, args, capture=True))
            path = "/v1/info" if service == "trino" else "/server_info"
            url = f"{'https' if https else 'http'}://{HOST}:{PORTS[service]}{path}"
            verify = str(ca) if https else True
            return probe(url, verify=verify, service=service, cluster=cluster, tls_port=tls_port, reason=reason)

        def records() -> list[dict[str, str]]:
            return break_glass.recent(cluster.kubectl, namespace, limit=20, context=cfg.kube_context) or []

        # Trusts the local CA and pins the ingress hostnames for the sign-in; its URL is unused.
        with (
            access.service_url(
                "shared/portal",
                service="portal",
                remote_port=80,
                namespace=namespace,
                log_path=f"/tmp/openlakeforge-{cfg.env}-outage-port-forward.log",
                shared_namespace=namespace,
                kube_context=cfg.kube_context,
                through_ingress=True,
            ),
            github_stub(cfg, {login: team}),
        ):
            try:
                timings = run_drill(
                    scale_dex=lambda n: kubectl(cfg, ["scale", *dex, f"--replicas={n}"]),
                    wait_dex=wait_dex,
                    signed_in=signed_in,
                    ingresses=ingresses,
                    still_protected=still_protected,
                    refused=refused,
                    reach=reach,
                    records=records,
                    replicas=replicas,
                    reason=reason,
                )
            except E2EError:
                logs_of_failure()
                raise
    log.info(
        f"Issuer-outage drill: first break-glass answer {timings.outage_to_break_glass_seconds:.0f}s after "
        f"Dex was down; sign-in worked {timings.restore_to_sign_in_seconds:.0f}s after Dex was Ready again."
    )


def probe(
    url: str, *, verify: str | bool, service: str, cluster: break_glass.Cluster, tls_port: int | None, reason: str
) -> int | None:
    """Open a recorded break-glass session to `service` and GET `url` through it once it answers."""
    answered: list[int] = []

    def reach(argv: Sequence[str], **_: object) -> None:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        try:
            wait_forwarding(process, PORTS[service], service)
            for _attempt in range(30):
                try:
                    status = requests.get(url, verify=verify, timeout=3).status_code
                    # An exited forward (e.g. the port was taken) means a stale listener answered, not this session.
                    if process.poll() is None:
                        answered.append(status)
                        return
                    raise E2EError(f"the break-glass port-forward to {service} exited early")
                except requests.RequestException:
                    time.sleep(1)
        finally:
            process.terminate()
            process.wait(timeout=10)

    break_glass.open_session(cluster, service, reason, 1, local_port=PORTS[service], tls_port=tls_port, run=reach)
    return answered[0] if answered else None


def wait_forwarding(process: subprocess.Popen[str], port: int, service: str, timeout: float = 30) -> None:
    """Block until kubectl says it listens on `port`, so only this forward can answer the probe."""
    want = f"Forwarding from 127.0.0.1:{port}"
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not select.select([process.stdout], [], [], 1)[0]:
            if process.poll() is not None:
                break
            continue
        line = process.stdout.readline()
        if want in line:
            return
        if not line and process.poll() is not None:
            break
    raise E2EError(f"the break-glass port-forward to {service} never reported {want!r}")
