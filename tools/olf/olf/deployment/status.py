"""Provider-neutral cluster status reporting.

Port of the `local-status` Make target (`kubectl get pods/svc/pvc`); kept
provider-neutral so #125 can reuse it verbatim for AWS/Azure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from olf.deployment.errors import DeploymentPreconditionError
from olf.tooling.kubectl import Kubectl

DEFAULT_RESOURCES: tuple[tuple[str, str], ...] = (
    ("Pods", "pods"),
    ("Services", "svc"),
    ("PVCs", "pvc"),
)


@dataclass(frozen=True)
class StatusSection:
    title: str
    output: str


@dataclass(frozen=True)
class StatusReport:
    sections: tuple[StatusSection, ...]
    urls: Mapping[str, str] = field(default_factory=dict)
    """User-facing URLs by contract ref, from the contract's ingress routes."""

    def render(self) -> str:
        blocks = [f"=== {section.title} ===\n{section.output}" for section in self.sections]
        if self.urls:
            blocks.insert(0, "=== URLs ===\n" + "\n".join(f"{ref}: {url}" for ref, url in sorted(self.urls.items())))
        return "\n\n".join(blocks)

    def as_dict(self) -> dict[str, object]:
        return {"urls": dict(self.urls), "sections": {section.title: section.output for section in self.sections}}


def collect_status(
    kubectl: Kubectl,
    *,
    namespaces: Sequence[str],
    context: str,
    kubeconfig: Path,
    env: Mapping[str, str] | None = None,
    resources: Sequence[tuple[str, str]] = DEFAULT_RESOURCES,
    urls: Mapping[str, str] | None = None,
) -> StatusReport:
    """Report one section per resource kind and namespace.

    A stage-aware deployment spreads its workloads over the shared platform
    namespace and one namespace per enabled stage, so the caller passes every
    namespace it owns rather than one."""
    sections = []
    for namespace in namespaces:
        for title, resource in resources:
            result = kubectl.get(
                resource,
                namespace=namespace,
                context=context,
                kubeconfig=kubeconfig,
                env=env,
                check=False,
            )
            if not result.ok:
                detail = result.stderr.strip() or f"kubectl exited {result.returncode}"
                raise DeploymentPreconditionError(f"failed to query {title} in namespace '{namespace}': {detail}")
            output = result.stdout.strip() or result.stderr.strip()
            sections.append(StatusSection(title=f"{title} ({namespace})", output=output))
    return StatusReport(sections=tuple(sections), urls=urls or {})
