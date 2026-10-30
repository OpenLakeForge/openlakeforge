"""Check a stage's source bindings before an activation rolls out (#546).

A source's `source.yaml` names the settings and credentials its loader reads
(`runtime.config` / `runtime.secrets`); the stage binds their values in a
ConfigMap and a Secret in its own namespace. A missing binding would only
surface when a run pod fails to start, so activation refuses first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from openlakeforge_domain.inventory import Source

from olf.common.tooling.kubectl import Kubectl

# Prints the key names of `.data` and nothing else, so a Secret's values never
# reach this process, its output or a log.
_KEY_NAMES = 'go-template={{range $key, $_ := .data}}{{$key}}{{"\\n"}}{{end}}'


def missing_source_bindings(
    sources: Iterable[Source],
    *,
    kubectl: Kubectl,
    namespace: str,
    context: str | None,
    kubeconfig: Path | None,
    env: Mapping[str, str] | None,
) -> list[str]:
    """What the stage lacks, one line per ConfigMap or Secret; empty when bound."""
    problems: list[str] = []
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
            if not result.ok:
                problems.append(f"source {source.name!r}: {kind} {name} is missing or unreadable in {namespace}")
                continue
            absent = [key for key in keys if key not in set(result.stdout.split())]
            if absent:
                problems.append(f"source {source.name!r}: {kind} {name} in {namespace} has no key {', '.join(absent)}")
    return problems
