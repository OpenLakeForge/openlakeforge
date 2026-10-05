"""Ingress drills (#268): Traefik restart and certificate renewal on the local stack.

Both drive the stage's Dagster route, the one route every stage has, and keep
what they observed under `.tmp/e2e-evidence/` for the workflow to upload.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import ssl
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from olf import access, config, k8s, log
from olf.contracts import CONTRACT_STAGE_ENV
from olf.e2e._dagster import DAGSTER_WEBSERVER_SERVICE_NAME
from olf.e2e._shell import E2EConfig, E2EError, kubectl

TRAEFIK_DEPLOYMENT = "deploy/traefik"
# cert-manager's ingress-shim names the Certificate after the Ingress's TLS
# secret (modules/access/traefik).
ROUTES_CERTIFICATE = "openlakeforge-routes-tls"
DRILL_TIMEOUT_SECONDS = 180


def check_traefik_restart_recovery(cfg: E2EConfig) -> None:
    if _skipped("the Traefik restart drill"):
        return
    log.step("Checking ingress routes recover from a Traefik restart...")
    started = time.monotonic()
    kubectl(cfg, ["rollout", "restart", TRAEFIK_DEPLOYMENT, "-n", cfg.platform_namespace])
    kubectl(
        cfg,
        ["rollout", "status", TRAEFIK_DEPLOYMENT, "-n", cfg.platform_namespace, f"--timeout={DRILL_TIMEOUT_SECONDS}s"],
    )
    with _route(cfg) as url:
        recovered = k8s.http_wait(url, attempts=DRILL_TIMEOUT_SECONDS // 2, delay=2)
    elapsed = round(time.monotonic() - started, 1)
    _retain(cfg, "traefik-restart", {"route": url, "recovered": recovered, "elapsed_seconds": elapsed})
    if not recovered:
        raise E2EError(f"{url} did not answer within {elapsed}s of Traefik restarting.")


def check_certificate_renewal(cfg: E2EConfig) -> None:
    """Force a renewal of the served wildcard certificate and require a new serial with no failed handshake.

    Renewal is triggered the way `cmctl renew` does it, an Issuing condition on
    the Certificate's status, so the old Secret keeps serving until the new
    one replaces it.
    """
    if _skipped("the certificate renewal drill"):
        return
    log.step("Checking the served route certificate renews without downtime...")
    with _route(cfg) as url:
        host, port = urlsplit(url).hostname or "", urlsplit(url).port or 443
        before = _served_serial(host, port)
        issuing = {
            "type": "Issuing",
            "status": "True",
            "reason": "ManuallyTriggered",
            "message": "olf e2e certificate renewal drill",
            "lastTransitionTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # Conditions are keyed by type, so a left-over Issuing (say, False
        # after a failed issuance) is replaced rather than duplicated.
        current = kubectl(
            cfg,
            ["get", "certificate", ROUTES_CERTIFICATE, "-n", cfg.namespace, "-o", "jsonpath={.status.conditions}"],
            capture=True,
        )
        conditions = [c for c in json.loads(current or "[]") if c.get("type") != "Issuing"] + [issuing]
        kubectl(
            cfg,
            [
                "patch", "certificate", ROUTES_CERTIFICATE, "-n", cfg.namespace, "--subresource=status",
                "--type=json", "-p", json.dumps([{"op": "add", "path": "/status/conditions", "value": conditions}]),
            ],
        )
        serial, handshakes, failures = before, 0, []
        deadline = time.monotonic() + DRILL_TIMEOUT_SECONDS
        while serial == before and time.monotonic() < deadline:
            time.sleep(1)
            handshakes += 1
            try:
                serial = _served_serial(host, port)
            except OSError as exc:  # ssl.SSLError included
                failures.append(str(exc))
    evidence = {
        "route": url,
        "certificate": f"{cfg.namespace}/{ROUTES_CERTIFICATE}",
        "serial_before": before,
        "serial_after": serial,
        "handshakes": handshakes,
        "failed_handshakes": failures,
    }
    _retain(cfg, "certificate-renewal", evidence)
    if failures:
        raise E2EError(f"{len(failures)} of {handshakes} TLS handshakes to {host} failed during renewal: {failures[0]}")
    if serial == before:
        raise E2EError(f"{host} still served serial {before} {DRILL_TIMEOUT_SECONDS}s after renewal was triggered.")


@contextlib.contextmanager
def _route(cfg: E2EConfig) -> Iterator[str]:
    with access.service_url(
        f"stage/{os.environ.get(CONTRACT_STAGE_ENV, 'dev')}/orchestration",
        service=DAGSTER_WEBSERVER_SERVICE_NAME,
        remote_port=80,
        namespace=cfg.namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-dagster-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
    ) as url:
        yield url


def _skipped(drill: str) -> bool:
    # OLF_PORT_FORWARD is the documented way to run e2e around a broken ingress.
    if config.truthy(os.environ.get(access.PORT_FORWARD_ENV, "")):
        log.info(f"Skipping {drill}: {access.PORT_FORWARD_ENV} bypasses the ingress.")
        return True
    return False


def _served_serial(host: str, port: int) -> str:
    # Inside `_route`, *.localhost resolves to loopback and REQUESTS_CA_BUNDLE
    # holds the local CA, so this verifies exactly what a client would.
    context = ssl.create_default_context(cafile=os.environ.get("REQUESTS_CA_BUNDLE"))
    with (
        socket.create_connection((host, port), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        return str((tls.getpeercert() or {})["serialNumber"])


def _retain(cfg: E2EConfig, drill: str, evidence: dict[str, Any]) -> None:
    path = cfg.repo_root / ".tmp/e2e-evidence" / f"{drill}-{cfg.namespace}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    log.info(f"{drill}: {json.dumps(evidence)} (kept in {path})")
