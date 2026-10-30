"""Workload-secret rotation (#178): `olf secrets rotate`.

Runs the documented procedure (docs/guides/identity/rotate-secrets.md, "Rotate
secrets") in one command. It reads a Secret's identity (uid and
resourceVersion) and whether a key exists, never a value, and passes no value
on any argv.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olf import log

# The credential Job creates a missing client Secret and never overwrites one,
# so replacing it is what makes it generate the deleted Secret again.
CREDENTIALS_JOB = "module.dex[0].kubernetes_job_v1.credentials"
# Targeted, so a rotation applies the Job and its dependencies only and cannot reconfigure the rest of the
# platform or touch project-owned deployments.
APPLY_ARGS = [f"-target={CREDENTIALS_JOB}", f"-replace={CREDENTIALS_JOB}"]
ROLLOUT_TIMEOUT = "300s"
GITHUB_APP = "github-app"
PORTAL_ROUTE = "shared/portal"
# Every Dex static client the shared Job writes. The Job creates what is missing, so rotating one client is only
# safe when the others exist: a missing sibling would be regenerated in the same run without its consumers restarted.
DEX_CLIENTS = ("perimeter", "trino", "superset", "openmetadata")
KEY_PRESENT = '{{if index .data "client-secret"}}yes{{end}}'


class RotationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Restart:
    resource: str
    selector: str | None = None

    def __str__(self) -> str:
        return f"{self.resource} -l {self.selector}" if self.selector else self.resource


DEX = Restart("deployment/dex")


@dataclass(frozen=True)
class Rotation:
    secret: str
    namespace: str
    restarted: tuple[Restart, ...]
    was_missing: bool = False


# Consumers the runbook names. Superset keeps a copy per stage and OpenMetadata
# has no documented consumer step yet, so neither is a target.
CLIENT_RESTARTS: Mapping[str, tuple[Restart, ...]] = {
    "dex-client-perimeter": (DEX, Restart("deployment", "openlakeforge.io/component=perimeter")),
    "dex-client-trino": (DEX, Restart("deployment/trino-coordinator")),
}
TARGETS = (*CLIENT_RESTARTS, GITHUB_APP)


def unknown_target_message(target: str) -> str:
    return f"unknown target {target!r}; expected one of: {', '.join(TARGETS)}"


def require_local_github_issuer(context: Any) -> None:
    """The rotation is the local Dex runbook: refuse any other provider or issuer before touching anything."""
    identity = context.topology.identity
    if context.provider.value != "local" or identity is None or identity.issuer != "github":
        raise RotationError("olf secrets rotate needs provider: local with identity.issuer: github")


def kube_args(context: Any) -> dict[str, Any]:
    return {
        "context": context.kube_context,
        "kubeconfig": context.paths.kubeconfig_path,
        "env": context.command_env(base=os.environ),
    }


def secret_identity(kubectl: Any, context: Any, name: str) -> str | None:
    """`uid/resourceVersion` of the Secret, or None when absent. The data is never requested."""
    out = kubectl.get(
        "secret",
        name=name,
        namespace=context.shared_namespace,
        output="jsonpath={.metadata.uid}/{.metadata.resourceVersion}",
        check=False,
        **kube_args(context),
    )
    return out.stdout.strip() or None


def has_client_secret_key(kubectl: Any, context: Any, name: str) -> bool:
    """Whether the Secret has the `client-secret` key. The template prints `yes`, never the value."""
    out = kubectl.get(
        "secret",
        name=name,
        namespace=context.shared_namespace,
        output=f"go-template={KEY_PRESENT}",
        check=False,
        **kube_args(context),
    )
    return out.stdout.strip() == "yes"


def _local_provider(context: Any) -> Any:
    from olf.deployment.engine import build_provider
    from olf.tooling.toolkit import Toolkit

    env = context.command_env(base=os.environ)
    return build_provider(context, toolkit=Toolkit.default(environ=env), environ=env)


def check_platform_state(context: Any) -> None:
    """Refuse on a drifted platform state, with the predicate the drift reset uses, before anything is deleted."""
    from olf.deployment.errors import DeploymentError, DeploymentPreconditionError
    from olf.deployment.local import platform

    provider = _local_provider(context)
    try:
        platform.require_foundation_state(provider.config)
    except DeploymentPreconditionError as exc:
        raise RotationError(f"{exc} Run olf deploy first.") from exc
    if not platform.platform_state_holds_core(provider.config, provider.tools, env=provider.env):
        raise RotationError("platform state has drifted; run olf deploy first")
    try:
        platform.prepare_targets(provider.config, provider.tools, env=provider.env)
    except DeploymentError as exc:
        raise RotationError(str(exc)) from exc


def builds_superset_image(context: Any) -> bool:
    """Whether this stack runs a locally built Superset image, which only `platform_up` sets and an apply drops."""
    import platform

    provider = _local_provider(context)
    if not provider.config.features.analytics_enabled:
        return False
    arch = provider.tools.docker.server_arch(env=provider.env) or platform.machine()
    return bool(provider.config.images.builds_superset(arch))


def apply_platform(context: Any, extra_args: list[str]) -> None:
    """Only the targeted Terraform apply, not the drift-recovering platform path."""
    from olf.deployment.errors import DeploymentError
    from olf.deployment.local import platform

    provider = _local_provider(context)
    try:
        platform.apply_targets(provider.config, provider.tools, env=provider.env, extra_args=extra_args)
    except DeploymentError as exc:
        raise RotationError(str(exc)) from exc


def portal_url(context: Any) -> str | None:
    """The portal's URL from the access contract, when it can be read."""
    from olf.contract_state import load_provider_contracts
    from olf.tooling.toolkit import Toolkit

    env = context.command_env(base=os.environ)
    try:
        directory = Path(env.get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", context.paths.platform_terraform_dir))
        tools = Toolkit.default(environ=env)
        contracts = load_provider_contracts(str(directory.resolve()), environ=env, resolver=tools.resolver)
        return str((contracts or {})["shared"]["access"]["routes"][PORTAL_ROUTE]["url"])
    except Exception:  # noqa: BLE001 - the URL only improves the closing line; the rotation is already done
        return None


def preflight(context: Any, target: str, *, kubectl: Any, platform_check: Callable[[Any], None]) -> None:
    """Everything that can refuse, before the first delete."""
    require_local_github_issuer(context)
    platform_check(context)
    # A missing target is recoverable (the Job creates it); an existing malformed one is not.
    if secret_identity(kubectl, context, target) is not None and not has_client_secret_key(kubectl, context, target):
        raise RotationError(f"Secret '{target}' lacks the key client-secret; run olf deploy first")
    for client in DEX_CLIENTS:
        sibling = f"dex-client-{client}"
        if sibling != target and not has_client_secret_key(kubectl, context, sibling):
            raise RotationError(
                f"Secret '{sibling}' is missing or lacks the key client-secret, and the shared Job would "
                "regenerate it too; run olf deploy first"
            )


def rotate_client(
    context: Any,
    target: str,
    *,
    kubectl: Any,
    platform_apply: Callable[[Any, list[str]], None] = apply_platform,
    platform_check: Callable[[Any], None] = check_platform_state,
) -> Rotation:
    if target not in CLIENT_RESTARTS:
        raise RotationError(unknown_target_message(target))
    preflight(context, target, kubectl=kubectl, platform_check=platform_check)
    namespace, kube = context.shared_namespace, kube_args(context)
    before = secret_identity(kubectl, context, target)
    if before is None:
        log.step(f"Secret '{target}' is missing; skipping the delete, the Job regenerates it.")
    else:
        log.step(f"Deleting Secret '{target}' (namespace '{namespace}')...")
        kubectl.delete("secret", target, namespace=namespace, **kube)
    log.step("Replacing the credential Job in a targeted platform apply, which creates the Secret again...")
    platform_apply(context, APPLY_ARGS)
    after = secret_identity(kubectl, context, target)
    if after is None or after == before:
        raise RotationError(f"Secret '{target}' was not regenerated by the platform apply")
    restarts = CLIENT_RESTARTS[target]
    restart_and_wait(context, restarts, kubectl=kubectl)
    return Rotation(target, namespace, restarts, was_missing=before is None)


def restart_and_wait(context: Any, restarts: tuple[Restart, ...], *, kubectl: Any) -> None:
    namespace, kube = context.shared_namespace, kube_args(context)
    for restart in restarts:
        log.step(f"Restarting {restart}...")
        kubectl.rollout_restart(restart.resource, namespace=namespace, selector=restart.selector, **kube)
    for restart in restarts:
        kubectl.rollout_status(
            restart.resource, namespace=namespace, selector=restart.selector, timeout=ROLLOUT_TIMEOUT, **kube
        )
