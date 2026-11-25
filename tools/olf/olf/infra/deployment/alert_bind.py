"""Write and report alert receiver webhook URLs (#1425).

The evaluator reads each receiver's URL through `secretKeyRef` from the Secret its `secret_ref` names, in the shared
namespace (ADR 0023, decision 4). This is the one writer; `olf infra deploy` reads the same Secrets through
`receiver_statuses`. Reports carry names and key names, never a value.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from olf.common.profile import AlertReceiver
from olf.common.tooling.kubectl import Kubectl
from olf.infra.deployment.source_bind import SourceBindingError, StageCluster, apply_manifest, stored_data

_KEYS = r'go-template={{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}'


class AlertBindingError(SourceBindingError):
    """A request the receiver declaration or the cluster rules out. Messages never carry a value."""


@dataclass(frozen=True)
class ReceiverStatus:
    receiver: str
    secret: str
    key: str
    # The Secret's key names, or None when kubectl could not read it.
    present: tuple[str, ...] | None

    @property
    def bound(self) -> bool:
        return self.present is not None and self.key in self.present


def receiver_statuses(
    receivers: Sequence[AlertReceiver],
    kubectl: Kubectl,
    *,
    namespace: str,
    context: str | None,
    kubeconfig: Path | None,
    env: Mapping[str, str] | None,
) -> list[ReceiverStatus]:
    """Per receiver, the key names its Secret holds. Each Secret is read once; no value is read."""
    read: dict[str, tuple[str, ...] | None] = {}
    statuses = []
    for receiver in receivers:
        ref = receiver.secret_ref
        if ref.name not in read:
            result = kubectl.get(
                "secret",
                name=ref.name,
                namespace=namespace,
                output=_KEYS,
                context=context,
                kubeconfig=kubeconfig,
                env=env,
                check=False,
            )
            read[ref.name] = tuple(result.stdout.split()) if result.ok else None
        statuses.append(ReceiverStatus(receiver.name, ref.name, ref.key, read[ref.name]))
    return statuses


def validate_url(value: str) -> None:
    """The evaluator's rule (`WebhookNotifier`): https only. The message never names the value."""
    scheme: str | None = None
    try:
        scheme = urlsplit(value).scheme
    except ValueError:
        pass  # Its message names the value, so it is neither shown nor chained.
    if scheme is None:
        raise AlertBindingError("the webhook URL is not a valid URL")
    if scheme != "https":
        raise AlertBindingError("the webhook URL must use https")


def bind_receiver(receiver: AlertReceiver, value: str, cluster: StageCluster, *, dry_run: bool) -> str:
    """Write `value` as the receiver's key, keeping every other key of a shared Secret; one report line."""
    validate_url(value)
    _require_namespace(cluster)
    ref = receiver.secret_ref
    stored = stored_data(cluster, "secret", ref.name)
    data = {**(stored or {}), ref.key: base64.b64encode(value.encode()).decode()}
    changed = stored is None or stored.get(ref.key) != data[ref.key]
    verb = "unchanged" if not changed else "create" if stored is None else "update"
    if changed and not dry_run:
        apply_manifest(cluster, _secret_manifest(ref.name, cluster.namespace, data), "secret", ref.name)
    outcome = verb if verb == "unchanged" else f"would {verb}" if dry_run else f"{verb}d"
    return f"receiver {receiver.name}: secret {ref.name} key {ref.key} in {cluster.namespace} ({outcome})"


def _require_namespace(cluster: StageCluster) -> None:
    """Never create the shared namespace: one made outside Terraform is torn down as drift by the first apply."""
    exists = cluster.kubectl.get(
        "namespace",
        name=cluster.namespace,
        context=cluster.context,
        kubeconfig=cluster.kubeconfig,
        env=cluster.env,
        check=False,
    ).ok
    if not exists:
        raise AlertBindingError(
            f"the shared namespace {cluster.namespace} does not exist yet: run `olf infra deploy` first; "
            "it creates the namespace and stops naming this command"
        )


def _secret_manifest(name: str, namespace: str, data: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/managed-by": "olf"}},
            "type": "Opaque",
            "data": dict(data),
        }
    )
