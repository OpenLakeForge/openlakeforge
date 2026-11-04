"""Check a stage's source bindings before an activation rolls out (#546).

A source's `source.yaml` names the settings and credentials its loader reads
(`runtime.config` / `runtime.secrets`); the stage binds their values in a
ConfigMap and a Secret in its own namespace. A missing binding would only
surface when a run pod fails to start, so activation refuses first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from openlakeforge_core.inventory import Source

from olf.common.tooling.kubectl import Kubectl

# Prints the key names of `.data` and nothing else, so a Secret's values never
# reach this process, its output or a log.
_KEY_NAMES = 'go-template={{range $key, $_ := .data}}{{$key}}{{"\\n"}}{{end}}'


@dataclass(frozen=True)
class BindingStatus:
    source: str
    kind: str
    name: str
    namespace: str
    unreadable: bool
    missing: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def problem(self) -> str:
        prefix = f"source {self.source!r}: {self.kind} {self.name}"
        if self.unreadable:
            return f"{prefix} is missing or unreadable in {self.namespace}"
        if self.missing:
            return f"{prefix} in {self.namespace} has no key {', '.join(self.missing)}"
        return ""


def source_binding_status(
    sources: Iterable[Source],
    *,
    kubectl: Kubectl,
    namespace: str,
    context: str | None,
    kubeconfig: Path | None,
    env: Mapping[str, str] | None,
) -> list[BindingStatus]:
    statuses: list[BindingStatus] = []
    for source in sources:
        for kind, name, keys in (
            ("configmap", source.config_map_name, source.runtime_config),
            ("secret", source.credentials_secret_name, source.runtime_secrets),
        ):
            if not keys:
                continue
            result = kubectl.get(
                kind,
                name=name,
                namespace=namespace,
                context=context,
                kubeconfig=kubeconfig,
                output=_KEY_NAMES,
                env=env,
                check=False,
            )
            present = set(result.stdout.split()) if result.ok else set()
            statuses.append(
                BindingStatus(
                    source=source.name,
                    kind=kind,
                    name=name,
                    namespace=namespace,
                    unreadable=not result.ok,
                    missing=tuple(key for key in keys if key not in present),
                    extra=tuple(sorted(present - set(keys))),
                )
            )
    return statuses


def missing_source_bindings(
    sources: Iterable[Source],
    *,
    kubectl: Kubectl,
    namespace: str,
    context: str | None,
    kubeconfig: Path | None,
    env: Mapping[str, str] | None,
) -> list[str]:
    return [
        status.problem
        for status in source_binding_status(
            sources, kubectl=kubectl, namespace=namespace, context=context, kubeconfig=kubeconfig, env=env
        )
        if status.problem
    ]
