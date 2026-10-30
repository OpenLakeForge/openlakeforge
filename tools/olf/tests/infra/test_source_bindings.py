from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from openlakeforge_domain.inventory import Source

from olf.infra.deployment.source_bindings import missing_source_bindings

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from libs.source_bindings import K8S_CONFIG_TAG, source_binding_tags  # noqa: E402

_API = Source(
    descriptor_path=Path("source.yaml"),
    name="crm_api",
    display_name="CRM API",
    resources=(),
    runtime_config=("base_url",),
    runtime_secrets=("api_token",),
)
_CSV = Source(descriptor_path=Path("source.yaml"), name="crm", display_name="CRM", resources=())


def test_a_job_carries_its_sources_bindings_as_key_references_only() -> None:
    config = json.loads(source_binding_tags([_API, _CSV])[K8S_CONFIG_TAG])

    assert config["container_config"]["env"] == [
        {
            "name": "SOURCES__CRM_API__BASE_URL",
            "valueFrom": {"configMapKeyRef": {"name": "olf-source-crm-api", "key": "base_url"}},
        },
        {
            "name": "SOURCES__CRM_API__API_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": "olf-source-crm-api-credentials", "key": "api_token"}},
        },
    ]


def test_a_job_whose_sources_need_no_bindings_carries_no_tag() -> None:
    assert source_binding_tags([_CSV]) == {}


class _Namespace:
    """`kubectl get` against a namespace, answering only with key names, as the check asks."""

    def __init__(self, objects: dict[tuple[str, str], dict[str, str]]) -> None:
        self.objects = objects
        self.outputs: list[str] = []

    def get(self, kind, *, name, namespace, output, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG002
        self.outputs.append(output)
        data = self.objects.get((kind, name))
        if data is None:
            return SimpleNamespace(ok=False, stdout="", stderr="NotFound")
        return SimpleNamespace(ok=True, stdout="".join(f"{key}\n" for key in data), stderr="")


def _check(namespace: _Namespace, *sources: Source) -> list[str]:
    return missing_source_bindings(
        sources or (_API,), kubectl=namespace, namespace="olf-prod", context=None, kubeconfig=None, env=None
    )


def test_a_fully_bound_stage_passes() -> None:
    namespace = _Namespace(
        {
            ("configmap", "olf-source-crm-api"): {"base_url": "https://prod.api.example"},
            ("secret", "olf-source-crm-api-credentials"): {"api_token": "s3cr3t"},
        }
    )

    assert _check(namespace) == []
    # Key names only: no output format that would print a value.
    assert all(output.startswith("go-template=") and ".data" in output for output in namespace.outputs)


def test_a_missing_secret_and_a_missing_key_are_both_reported_without_values() -> None:
    namespace = _Namespace({("configmap", "olf-source-crm-api"): {"other": "https://prod.api.example"}})

    problems = _check(namespace)

    assert problems == [
        "source 'crm_api': configmap olf-source-crm-api in olf-prod has no key base_url",
        "source 'crm_api': secret olf-source-crm-api-credentials is missing or unreadable in olf-prod",
    ]
    assert not any("prod.api.example" in problem for problem in problems)


def test_a_source_with_config_only_needs_no_secret() -> None:
    config_only = Source(
        descriptor_path=Path("source.yaml"), name="feed", display_name="Feed", resources=(), runtime_config=("url",)
    )
    namespace = _Namespace({("configmap", "olf-source-feed"): {"url": "https://feed.example"}})

    assert _check(namespace, config_only) == []
    assert len(namespace.outputs) == 1
