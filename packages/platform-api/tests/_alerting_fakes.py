"""An in-memory ops bucket for the alert evaluator's tests."""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError
from openlakeforge_core.project_activation import ProjectActivation, activation_key, active_key, pointer_document
from openlakeforge_core.project_revision import ComponentEntries, aggregate_components, revision_prefix, sidecar_key


class FakeBucket:
    """get_object and put_object over a dict, as boto3 answers them; `down` raises like an unreachable store."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.down = False
        # Key prefixes whose writes fail, to stand for a store that fails part-way through a run.
        self.failing_writes: tuple[str, ...] = ()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._check()
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        self._check()
        if Key.startswith(self.failing_writes) and self.failing_writes:
            raise EndpointConnectionError(endpoint_url="http://store.invalid")
        self.objects[Key] = Body

    def list_objects_v2(  # noqa: N803
        self, *, Bucket: str, Prefix: str = "", ContinuationToken: str = ""
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _check(self) -> None:
        if self.down:
            raise EndpointConnectionError(endpoint_url="http://store.invalid")

    def under(self, prefix: str) -> dict[str, dict[str, Any]]:
        return {key: json.loads(body) for key, body in sorted(self.objects.items()) if key.startswith(prefix)}


def settings_mapping() -> dict[str, Any]:
    return {
        "stages": ["dev"],
        "dagster_graphql_urls": {"dev": "http://dagster-dev/graphql"},
        "state_base_uri": "s3://ops/alerts/",
        "receivers": [{"name": "ops", "url_env": "OPS_URL"}],
    }


GOVERNANCE = "domains:\n  - name: sales\n    products:\n      - id: order_revenue\n"
POLICY = """\
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy
routes:
  on-call: ops
defaults:
  notify: [on-call]
rules:
  - match: {domain: "*", product: "*"}
    when:
      run_failed: {}
"""


EVERYTHING = POLICY.replace(
    "      run_failed: {}\n",
    "      run_failed: {}\n      missed_run: {}\n      freshness: {}\n      quality: {}\n      schedule_mismatch: {}\n",
)


def _bytes(content: str | bytes) -> bytes:
    return content if isinstance(content, bytes) else content.encode()


def _sha(content: str | bytes) -> str:
    return hashlib.sha256(_bytes(content)).hexdigest()


def publish(
    bucket: FakeBucket,
    *,
    stage: str = "dev",
    alerts: str | bytes | None = POLICY,
    governance: str = GOVERNANCE,
    seed: str = "a",
    activate: bool = True,
) -> ProjectActivation:
    """A published revision and, unless `activate` is false, the stage's ACTIVE.json naming its activation."""
    files: dict[str, str | bytes] = {"lakehouse_code/governance.yml": governance}
    if alerts is not None:
        files["lakehouse_code/alerts.yml"] = alerts
    components = [
        ComponentEntries("descriptors", {name: _sha(content) for name, content in files.items()}),
        ComponentEntries("image", {"project-code": f"x@sha256:{seed * 64}"}),
    ]
    revision = aggregate_components(components)
    sidecar = {
        "apiVersion": "openlakeforge.io/v1alpha1",
        "kind": "ProjectRevision",
        "schema_version": 2,
        "components": [component.to_dict() for component in components],
        "revision": revision,
    }
    bucket.objects[sidecar_key(revision)] = json.dumps(sidecar).encode()
    for name, content in files.items():
        bucket.objects[f"{revision_prefix(revision)}/descriptors/{name}"] = _bytes(content)
    activation = ProjectActivation(
        deployment_profile="example",
        provider="local",
        stage=stage,  # type: ignore[arg-type]
        project_name="widgets",
        project_revision=revision,
        distribution_version="0.3.0",
        project_code_image=f"x@sha256:{seed * 64}",
        provider_binding_digest="sha256:" + "d" * 64,
        capabilities={"analytics": True},
    ).resolved()
    bucket.objects[activation_key(activation.activation_revision, stage=stage)] = activation.to_json().encode()
    if activate:
        bucket.objects[active_key(stage)] = pointer_document(activation.activation_revision)
    return activation


def declare_everything(bucket: FakeBucket, stages: tuple[str, ...]) -> None:
    """Give each stage that has no activation a policy that turns every product condition on. Alerts are silent
    unless declared, and most evaluator tests are about incidents, not the policy."""
    for stage in stages:
        if active_key(stage) not in bucket.objects:
            publish(bucket, stage=stage, alerts=EVERYTHING)
