"""Break-glass check (#177): one recorded use with Dex and the perimeter up.

The path must work without the ingress and leave no trace beyond its record,
so the check compares the Ingress list before and after and reads the record.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence

import requests

from olf import break_glass, log
from olf.deployment.engine import Toolkit
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

REASON = "e2e break-glass check"
LOCAL_PORT = 15556


def check_break_glass(cfg: E2EConfig) -> None:
    contracts = load_provider_contracts_or_raise(cfg)
    if not (contracts["shared"]["access"].get("perimeter") or {}).get("enabled"):
        log.info("Skipping the break-glass check: the contract declares no enabled perimeter.")
        return
    log.step("Checking a recorded break-glass use to Dex with the perimeter enabled...")
    ingresses = kubectl(cfg, ["get", "ingress", "-A", "-o", "name"], capture=True)
    answered: list[int] = []

    def reach(argv: Sequence[str], **_: object) -> None:
        # The real port-forward, probed once; terminating it is the exit a Ctrl-C would be.
        process = subprocess.Popen(argv)
        try:
            for _attempt in range(30):
                try:
                    url = f"http://127.0.0.1:{LOCAL_PORT}/.well-known/openid-configuration"
                    answered.append(requests.get(url, timeout=3).status_code)
                    return
                except requests.RequestException:
                    time.sleep(1)
        finally:
            process.terminate()
            process.wait(timeout=10)

    cluster = break_glass.Cluster(
        Toolkit.default().kubectl, cfg.kube_context, None, cfg.platform_namespace, cfg.namespace
    )
    name = break_glass.open_session(cluster, "dex", REASON, 1, local_port=LOCAL_PORT, run=reach)
    if answered != [200]:
        raise E2EError(f"Dex did not answer through the break-glass path (statuses {answered}).")
    records = break_glass.recent(cluster.kubectl, cfg.platform_namespace, context=cfg.kube_context) or []
    mine = next((r for r in records if r.get("reason") == REASON and r.get("ended")), None)
    if not mine or not mine.get("user") or "duration_seconds" not in mine:
        raise E2EError(f"No complete break-glass record {name} in {cfg.platform_namespace}: {records}")
    after = kubectl(cfg, ["get", "ingress", "-A", "-o", "name"], capture=True)
    if after != ingresses:
        raise E2EError("Break-glass changed the Ingress set.")
