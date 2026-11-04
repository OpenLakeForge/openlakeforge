"""Write a stage's source bindings: the ConfigMap and Secret `source.yaml`'s `runtime` block names (#760).

`missing_source_bindings` reads them at activation; this is the one writer, so the object names, the key
names and the labels have one definition. Reports carry key names only, never values.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from openlakeforge_core.inventory import Source

from olf.common.deployment.errors import ToolingError
from olf.common.tooling.kubectl import Kubectl


class SourceBindingError(Exception):
    """A request the source's `runtime` block or the cluster rules out. Messages carry key names only."""


@dataclass(frozen=True)
class StageCluster:
    kubectl: Kubectl
    namespace: str
    context: str | None
    kubeconfig: Path | None
    env: Mapping[str, str] | None


@dataclass(frozen=True)
class _Object:
    resource: str
    api_kind: str
    name: str
    declared: tuple[str, ...]
    given: Mapping[str, str]
    encode: Callable[[str], str]


def _objects(source: Source, config: Mapping[str, str], secrets: Mapping[str, str]) -> list[_Object]:
    objects = [
        _Object("configmap", "ConfigMap", source.config_map_name, source.runtime_config, config, str),
        _Object(
            "secret",
            "Secret",
            source.credentials_secret_name,
            source.runtime_secrets,
            secrets,
            lambda value: base64.b64encode(value.encode()).decode(),
        ),
    ]
    return [obj for obj in objects if obj.declared]


def validate_keys(source: Source, config: Mapping[str, str], secrets: Mapping[str, str], *, partial: bool) -> None:
    if not (source.runtime_config or source.runtime_secrets):
        raise SourceBindingError(f"source {source.name!r} declares no `runtime` block, so it needs no binding")
    config_keys = ", ".join(source.runtime_config) or "(none)"
    secret_keys = ", ".join(source.runtime_secrets) or "(none)"
    declared = f"config: {config_keys}; secrets: {secret_keys}"
    for given, own, other, label in (
        (config, source.runtime_config, source.runtime_secrets, "a secret"),
        (secrets, source.runtime_secrets, source.runtime_config, "a config key"),
    ):
        for key in given:
            if key in other:
                raise SourceBindingError(f"{key!r} is {label} of source {source.name!r}; pass it with the other option")
            if key not in own:
                raise SourceBindingError(f"source {source.name!r} declares no key {key!r} ({declared})")
    if partial:
        return
    absent = [key for key in source.runtime_config if key not in config]
    absent += [key for key in source.runtime_secrets if key not in secrets]
    if absent:
        raise SourceBindingError(
            f"source {source.name!r} needs {', '.join(absent)}; pass them, or --partial to keep the keys already bound"
        )


def _stored(obj: _Object, cluster: StageCluster) -> dict[str, str] | None:
    """The object's `.data` as stored (base64 for a Secret), or None when it does not exist."""
    result = cluster.kubectl.get(
        obj.resource,
        name=obj.name,
        namespace=cluster.namespace,
        context=cluster.context,
        kubeconfig=cluster.kubeconfig,
        output="json",
        env=cluster.env,
        check=False,
    )
    if not result.ok:
        if "NotFound" in result.stderr:
            return None
        raise SourceBindingError(f"could not read {obj.resource} {obj.name} in {cluster.namespace}")
    return dict(json.loads(result.stdout).get("data") or {})


def bind(
    source: Source,
    config: Mapping[str, str],
    secrets: Mapping[str, str],
    cluster: StageCluster,
    *,
    partial: bool,
    dry_run: bool,
) -> list[str]:
    """Create or update the source's objects; one report line each, key names only.

    The manifest goes to kubectl on stdin, so a Secret's values never reach argv, a file or the report.
    Server-side apply drops a key this manager applied earlier and omits now, so `--partial` sends the stored
    value of each declared key it was not given back unchanged (read, never printed).
    """
    validate_keys(source, config, secrets, partial=partial)
    lines: list[str] = []
    for obj in _objects(source, config, secrets):
        stored = _stored(obj, cluster)
        data = {key: stored[key] for key in obj.declared if partial and stored and key in stored}
        data.update({key: obj.encode(value) for key, value in obj.given.items()})
        if not data:
            continue
        changed = stored is None or any(stored.get(key) != value for key, value in data.items())
        verb = "unchanged" if not changed else "create" if stored is None else "update"
        if changed and not dry_run:
            try:
                cluster.kubectl.apply_from_manifest(
                    _manifest(obj, source, cluster.namespace, data),
                    context=cluster.context,
                    kubeconfig=cluster.kubeconfig,
                    env=cluster.env,
                )
            except ToolingError:
                # Admission errors can echo the submitted Secret's data.
                raise SourceBindingError(
                    f"could not apply {obj.resource} {obj.name} in {cluster.namespace}; "
                    "check cluster permissions and policy"
                ) from None
        outcome = verb if verb == "unchanged" else f"would {verb}" if dry_run else f"{verb}d"
        lines.append(f"{obj.resource} {obj.name}: {', '.join(data)} ({outcome})")
    return lines


def unbind(source: Source, cluster: StageCluster) -> list[str]:
    lines = []
    # Retired runtime keys must not leave credentials that this command cannot remove.
    for resource, name in (("configmap", source.config_map_name), ("secret", source.credentials_secret_name)):
        result = cluster.kubectl.delete(
            resource,
            name,
            namespace=cluster.namespace,
            context=cluster.context,
            kubeconfig=cluster.kubeconfig,
            env=cluster.env,
        )
        lines.append(f"{resource} {name}: {'deleted' if result.stdout.strip() else 'absent'}")
    return lines


def _manifest(obj: _Object, source: Source, namespace: str, data: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": obj.api_kind,
            "metadata": {
                "name": obj.name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": "olf", "openlakeforge.io/source": source.name},
            },
            **({"type": "Opaque"} if obj.resource == "secret" else {}),
            "data": dict(data),
        }
    )
