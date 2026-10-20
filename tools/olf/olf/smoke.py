"""Bounded local smoke orchestration without Make or shell subprocesses."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from olf import config, e2e, log
from olf.deployment import contract_env
from olf.deployment.context import DeploymentContext, Profile
from olf.deployment.engine import DeploymentEngine, DeploymentPhase, Toolkit, build_provider
from olf.deployment.errors import DeploymentError


class SmokeError(RuntimeError):
    """The bounded smoke deployment failed or exceeded its deadline."""


def run(
    *,
    timeout_seconds: int,
    environ: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Deploy slim local, then run its smoke suite under one elapsed-time budget."""
    if timeout_seconds <= 0:
        raise SmokeError("smoke timeout must be greater than zero seconds")
    started = monotonic()
    env = dict(environ or os.environ)
    root = config.repo_root()
    cluster_name = env.get("CLUSTER_NAME", "openlakeforge-local")
    kubeconfig_path = Path(env.get("LOCAL_KUBECONFIG_PATH", root / ".tmp/kubeconfigs/local.yaml"))
    # No namespace override: the local root derives its namespaces from the
    # resolved topology, and the smoke suite must target what it deployed.
    context = DeploymentContext.local(
        repo_root=root,
        profile=Profile.SLIM,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
    )
    try:
        with _deadline(timeout_seconds):
            with patch.dict(os.environ, env, clear=False):
                log.step("Deploying slim local platform...")
                provider = build_provider(context, toolkit=Toolkit.default(), environ=env)
                DeploymentEngine(provider).deploy(DeploymentPhase.ALL)
                _require_remaining(
                    started, timeout_seconds, monotonic, "validating one product pipeline and Gold table"
                )
                log.step("Validating one product pipeline and Gold table...")
                # e2e.run() reads its own runtime config (e.g.
                # OPENLAKEFORGE_QUERY_TRINO_CATALOG) from the applied
                # provider-contract environment, not from anything
                # DeploymentEngine.deploy() exports - commands/e2e.py's CLI
                # path always wraps its own e2e.run() call the same way.
                # An explicit KUBECONFIG must still win over the context's
                # own resolved path (commands/e2e.py's
                # `resolve_kubeconfig_path`), or this unconditionally
                # overwrites it back to the context default.
                kubeconfig_override = config.env("KUBECONFIG")
                contract_terraform_dir = Path(
                    config.env("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(context.paths.platform_terraform_dir))
                )
                with contract_env.applied_contract_environment(
                    contract_terraform_dir=contract_terraform_dir,
                    repo_root=root,
                    namespace=context.namespace,
                    kube_context=context.kube_context,
                    kubeconfig_path=Path(kubeconfig_override) if kubeconfig_override else context.paths.kubeconfig_path,
                    port_forward_log_prefix=context.paths.port_forward_log_prefix,
                    environ=env,
                    topology=context.topology,
                    stage=context.stage,
                ):
                    e2e.run(
                        "local",
                        suite="smoke",
                        namespace=context.namespace,
                        shared_namespace=context.shared_namespace,
                        kube_context=context.kube_context,
                        repo_root=root,
                    )
    except DeploymentError as exc:
        raise SmokeError(str(exc)) from exc
    _require_remaining(started, timeout_seconds, monotonic, "completing smoke validation")
    log.info(f"Local slim smoke passed in {int(monotonic() - started)}s (budget: {timeout_seconds}s).")


def _require_remaining(started: float, budget: int, monotonic: Callable[[], float], phase: str) -> None:
    if monotonic() - started >= budget:
        raise SmokeError(f"Local slim smoke exceeded its {budget}s time budget during: {phase}.")


@contextmanager
def _deadline(timeout_seconds: int):
    """Interrupt blocking in-process deploy/e2e work at the smoke deadline."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)

    def _expired(_signum: int, _frame: object) -> None:
        raise SmokeError(f"Local slim smoke exceeded its {timeout_seconds}s time budget.")

    signal.signal(signal.SIGALRM, _expired)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)
