"""Shared E2E configuration, error type, and process/kubectl primitives.

Every other `olf.e2e` submodule depends on this one; it has no dependency on
any of them, so it stays a true leaf and keeps the package's import graph
acyclic.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openlakeforge_domain import LakehouseInventory

from olf import contracts, log
from olf.tooling.resolver import build_resolver
from olf.tooling.terraform import external_state_options

Environment = Literal["local", "azure", "aws"]
Suite = Literal["full", "smoke"]
Layer = Literal["governance", "analytics"]

KUBECTL_READ_RETRY_ATTEMPTS = 4
KUBECTL_READ_RETRY_DELAY_SECONDS = 2
TRANSIENT_KUBECTL_ERROR_MARKERS = (
    "tls handshake timeout",
    "i/o timeout",
    "connection reset by peer",
    "http2: client connection lost",
    "the server is currently unable to handle the request",
)


class E2EError(RuntimeError):
    pass


@dataclass(frozen=True)
class E2EConfig:
    env: Environment
    suite: Suite
    namespace: str
    kube_context: str
    repo_root: Path
    distribution_root: Path
    foundation_terraform_dir: Path | None
    contract_terraform_dir: Path
    inventory: LakehouseInventory
    shared_namespace: str = ""
    aws_region: str | None = None
    dagster_local_port: int | None = None
    superset_local_port: int | None = None
    openmetadata_local_port: int | None = None
    seaweedfs_local_port: int | None = None

    @property
    def platform_namespace(self) -> str:
        """The namespace owning the shared platform services.

        Trino, Polaris, SeaweedFS, and OpenMetadata are deployed once per
        cluster (ADR 0011), while Dagster and Superset are stage-scoped and
        live in `namespace`. The cloud POC roots are still single-namespace,
        so an unset `shared_namespace` falls back to the stage namespace.
        """
        return self.shared_namespace or self.namespace


def _kubectl_executable() -> str:
    """The resolved (possibly managed, #127) kubectl executable path.

    A separate function so tests can substitute a literal `"kubectl"`
    without exercising real toolchain resolution.
    """
    return str(build_resolver().resolve("kubectl"))


def _terraform_executable() -> str:
    """The resolved (possibly managed, #127) terraform executable path. See
    `_kubectl_executable`."""
    return str(build_resolver().resolve("terraform"))


def kubectl(
    cfg: E2EConfig,
    args: list[str],
    *,
    capture: bool = False,
    retry_transient: bool = False,
) -> str:
    command = [_kubectl_executable(), "--context", cfg.kube_context, *args]
    if retry_transient:
        return _run_retry_transient_kubectl(command, capture=capture)
    return _run(command, capture=capture)


def _run(args: list[str], *, capture: bool = False, env: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(args, capture_output=capture, text=True, check=False, env=env)
    if result.returncode != 0:
        detail = (result.stderr if capture else "") or ""
        raise E2EError(f"{' '.join(args)} failed: {detail.strip()}")
    return result.stdout if capture else ""


def _run_retry(args: list[str], *, capture: bool = False, attempts: int = 3, delay: float = 2.0) -> str:
    last_error: E2EError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _run(args, capture=capture)
        except E2EError as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(delay)
    if last_error is None:
        raise E2EError(f"{' '.join(args)} failed.")
    raise last_error


def _run_retry_transient_kubectl(
    args: list[str],
    *,
    capture: bool = False,
    attempts: int = KUBECTL_READ_RETRY_ATTEMPTS,
    delay: float = KUBECTL_READ_RETRY_DELAY_SECONDS,
) -> str:
    for attempt in range(1, attempts + 1):
        try:
            return _run(args, capture=capture)
        except E2EError as exc:
            if attempt == attempts or not is_transient_kubectl_error(exc):
                raise
            log.warn(
                f"Transient Kubernetes API error during read-only Trino probe "
                f"(attempt {attempt}/{attempts}); retrying..."
            )
            time.sleep(delay)
    raise E2EError(f"{' '.join(args)} failed.")  # pragma: no cover


def is_transient_kubectl_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in TRANSIENT_KUBECTL_ERROR_MARKERS)


def _terraform_output_args(terraform_dir: Path, *, mode: str, name: str) -> tuple[list[str], dict[str, str] | None]:
    """Build a `terraform output` argv/env pair for the AWS/Azure foundation
    reads in this module - the same external-state translation
    `Terraform._run`/`olf.contracts.load_provider_contracts` apply, or an
    installed distribution's foundation state under `OLF_HOME` is never
    found (see `olf.tooling.terraform.external_state_options`).
    """
    overlay, state_path = external_state_options(terraform_dir, os.environ, create=False)
    args = [_terraform_executable(), f"-chdir={terraform_dir}", "output"]
    if state_path is not None:
        args.append(f"-state={state_path}")
    args.extend([f"-{mode}", name])
    env = {**os.environ, **overlay} if overlay else None
    return args, env


def terraform_output(terraform_dir: Path | None, name: str) -> str:
    if terraform_dir is None:
        raise E2EError(f"cannot read Terraform output {name}: no Terraform directory configured.")
    args, env = _terraform_output_args(terraform_dir, mode="raw", name=name)
    return _run(args, capture=True, env=env).strip()


def load_provider_contracts_or_raise(cfg: E2EConfig) -> dict[str, Any]:
    provider_contracts = contracts.load_provider_contracts(str(cfg.contract_terraform_dir))
    if provider_contracts is None:
        raise E2EError(f"could not load provider_contracts from {cfg.contract_terraform_dir}")
    return provider_contracts


def aws_stack_region(cfg: E2EConfig) -> str:
    if cfg.foundation_terraform_dir is not None:
        return terraform_output(cfg.foundation_terraform_dir, "aws_region")
    if cfg.aws_region:
        return cfg.aws_region
    if os.environ.get("AWS_REGION"):
        return os.environ["AWS_REGION"]
    raise E2EError("AWS e2e requires a stack region from Terraform output or AWS_REGION.")
