"""Provider-neutral contract-environment activation.

Replaces sourcing `scripts/contracts/load-runtime-env.sh`: calls
`olf.common.contract_state.load_provider_contracts`/`build_contract_env` directly and
applies the exports/unsets to `os.environ` for the duration of a block,
restoring the previous values on exit. This is the in-process bridge to the
existing `os.environ`-reading library modules (`olf.infra.k8s`, `olf.common.config`,
`olf.infra.s3`, `olf.common.layers`) that every provider's artifact deployment needs, so
it lives at `olf.common.deployment` rather than under `olf.common.deployment.local` -
issue #125's AWS/Azure providers reuse it unmodified.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from olf.common import contract_state
from olf.infra import contracts

if TYPE_CHECKING:
    from olf.common.profile import DeploymentTopology, StageName


# The provider's command environment scopes DOCKER_CONFIG to an OLF-owned
# directory. Applying it to os.environ would make it indistinguishable from the
# developer's own, so `ambient_registry_env` would hand it to a pull from a
# private registry and the pull would run without their `docker login`.
_PROVIDER_SCOPED = frozenset({"DOCKER_CONFIG"})


@dataclass(frozen=True, eq=False)
class ContractEnvironment(Mapping[str, str]):
    """The resolved contract environment: a read-only `Mapping` over `environ`
    plus the stage coordinates it was resolved for. Existing dict consumers
    keep working because it compares and iterates like the mapping it wraps."""

    environ: Mapping[str, str]
    namespace: str
    kube_context: str
    kubeconfig_path: Path
    stage: StageName | str | None
    unsets: tuple[str, ...]

    def __getitem__(self, key: str) -> str:
        return self.environ[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.environ)

    def __len__(self) -> int:
        return len(self.environ)


@contextmanager
def applied_contract_environment(
    *,
    contract_terraform_dir: Path,
    repo_root: Path,
    namespace: str,
    kube_context: str,
    kubeconfig_path: Path,
    port_forward_log_prefix: Path,
    environ: Mapping[str, str] | None = None,
    topology: DeploymentTopology | None = None,
    stage: StageName | str | None = None,
) -> Iterator[ContractEnvironment]:
    """`topology` and `stage` select which stage a v3 contract resolves to.
    They are inert for the v2 payload the roots export today, which carries
    one deployment's worth of bindings and no stage index."""
    # Contract reads are Terraform operations too.  Start with the process
    # environment and overlay the provider's scoped command environment so an
    # installed wheel uses its extracted catalog and external state rather
    # than resolving either from the current checkout.
    base = dict(os.environ)
    if environ is not None:
        base.update(environ)
    provider_contracts = (
        contract_state.load_provider_contracts(str(contract_terraform_dir), environ=base)
        if environ
        else contract_state.load_provider_contracts(str(contract_terraform_dir))
    )
    exports, unsets = contracts.build_contract_env(
        base, provider_contracts, repo_root=repo_root, topology=topology, stage=stage
    )

    extra = {
        "OPENLAKEFORGE_REPO_ROOT": str(repo_root),
        "OPENLAKEFORGE_PROJECT_ROOT": str(repo_root),
        "NAMESPACE": namespace,
        "KUBE_CONTEXT": kube_context,
        "KUBECONFIG": str(kubeconfig_path),
        "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX": str(port_forward_log_prefix),
    }

    previous: dict[str, str | None] = {}
    applied = {key: value for key, value in {**base, **exports, **extra}.items() if key not in _PROVIDER_SCOPED}
    for name, value in applied.items():
        previous[name] = os.environ.get(name)
        os.environ[name] = value
    for name in unsets:
        previous.setdefault(name, os.environ.get(name))
        os.environ.pop(name, None)

    try:
        yield ContractEnvironment(
            environ=MappingProxyType(dict(os.environ)),
            namespace=namespace,
            kube_context=kube_context,
            kubeconfig_path=kubeconfig_path,
            stage=stage,
            unsets=tuple(unsets),
        )
    finally:
        for name, prior in previous.items():
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior
