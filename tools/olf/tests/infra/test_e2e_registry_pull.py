from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e import _registry_pull as registry_pull
from olf.infra.e2e._shell import E2EError

REPOSITORY = "ghcr.io/acme/lakehouse-code"
IMAGE = f"{REPOSITORY}@sha256:{'a' * 64}"
SECRET = "ghcr-pull"


def _deployment(name: str, *, secrets: tuple[str, ...] = (SECRET,), image: str = IMAGE) -> dict[str, Any]:
    pod = {"containers": [{"image": image}], "imagePullSecrets": [{"name": item} for item in secrets]}
    return {"kind": "Deployment", "metadata": {"name": name}, "spec": {"template": {"spec": pod}}}


def _cronjob(*, secrets: tuple[str, ...]) -> dict[str, Any]:
    pod = {"containers": [{"image": IMAGE}], "imagePullSecrets": [{"name": item} for item in secrets]}
    template = {"spec": {"template": {"spec": pod}}}
    return {"kind": "CronJob", "metadata": {"name": "log-archive"}, "spec": {"jobTemplate": template}}


def test_every_kind_of_workload_running_the_image_must_list_the_secret() -> None:
    items = [
        _deployment("code-location"),
        _cronjob(secrets=()),
        {"kind": "Pod", "metadata": {"name": "run-pod"}, "spec": {"containers": [{"image": IMAGE}]}},
        _deployment("postgres", secrets=(), image="docker.io/library/postgres:16"),
    ]

    missing, users = registry_pull.missing_pull_secret(items, IMAGE, SECRET)

    assert missing == ["CronJob/log-archive", "Pod/run-pod"]
    assert users == 3


def test_a_different_secret_does_not_count() -> None:
    missing, _ = registry_pull.missing_pull_secret([_deployment("code", secrets=("other",))], IMAGE, SECRET)

    assert missing == ["Deployment/code"]


class _Response:
    def __init__(self, status: int, *, headers: dict[str, str] | None = None, body: dict[str, str] | None = None):
        self.status_code = status
        self.headers = headers or {}
        self.ok = status < 400
        self._body = body or {}

    def json(self) -> dict[str, str]:
        return self._body


class _Registry:
    """An anonymous-token registry: the manifest needs a Bearer token, and `public` decides if one is granted."""

    def __init__(self, *, public: bool) -> None:
        self.public = public

    def get(self, url: str, *, headers: dict[str, str] | None = None, params: Any = None, timeout: int = 0) -> Any:
        if "token" in url:
            return _Response(200, body={"token": "t"}) if self.public else _Response(403)
        if (headers or {}).get("Authorization") == "Bearer t":
            return _Response(200)
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
        return _Response(401, headers={"WWW-Authenticate": challenge})


def test_a_registry_that_grants_an_anonymous_token_is_public() -> None:
    assert registry_pull.anonymous_manifest_status(IMAGE, session=_Registry(public=True)) == 200  # type: ignore[arg-type] # test double
    assert registry_pull.anonymous_manifest_status(IMAGE, session=_Registry(public=False)) == 403  # type: ignore[arg-type] # test double


def test_an_unreachable_registry_fails_the_check_rather_than_passing_it() -> None:
    class Down:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise requests.ConnectionError("no route")

    with pytest.raises(E2EError, match="could not tell whether"):
        registry_pull.anonymous_manifest_status(IMAGE, session=Down())  # type: ignore[arg-type] # test double


class Cluster:
    """What `kubectl get` returns for a stage: a code-location Deployment and the dagster ServiceAccount."""

    def __init__(self, *, deployment_secrets: tuple[str, ...] = (SECRET,), account: str = SECRET) -> None:
        self.deployment_secrets = deployment_secrets
        self.account = account

    def kubectl(self, cfg: object, args: list[str], *, capture: bool = False, **_: object) -> str:
        if args[1] == "serviceaccount":
            return self.account
        return json.dumps({"items": [_deployment("code-location", secrets=self.deployment_secrets)]})


@pytest.fixture
def patch_cluster(monkeypatch: pytest.MonkeyPatch) -> Callable[[Cluster], None]:
    def install(cluster: Cluster) -> None:
        monkeypatch.setattr(registry_pull, "kubectl", cluster.kubectl)

    monkeypatch.setattr(registry_pull, "_active_image", lambda cfg, stage: IMAGE)
    monkeypatch.setattr(registry_pull, "_pull_secret_name", lambda cfg: SECRET)
    monkeypatch.setattr(registry_pull, "anonymous_manifest_status", lambda image: 403)
    return install


def _cfg() -> Any:
    return SimpleNamespace(namespace="olf-dev", environ={CONTRACT_STAGE_ENV: "dev"})


def test_a_private_image_whose_pods_list_the_secret_passes(patch_cluster: Callable[[Cluster], None]) -> None:
    patch_cluster(Cluster())

    registry_pull.check(_cfg())


def test_a_deployment_without_the_secret_fails(patch_cluster: Callable[[Cluster], None]) -> None:
    patch_cluster(Cluster(deployment_secrets=()))

    with pytest.raises(E2EError, match=r"Deployment/code-location"):
        registry_pull.check(_cfg())


def test_floe_jobs_inherit_from_the_service_account_so_it_must_list_the_secret(
    patch_cluster: Callable[[Cluster], None],
) -> None:
    patch_cluster(Cluster(account=""))

    with pytest.raises(E2EError, match="ServiceAccount/dagster"):
        registry_pull.check(_cfg())


def test_a_public_image_fails_unless_it_is_allowed(
    patch_cluster: Callable[[Cluster], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_cluster(Cluster())
    monkeypatch.setattr(registry_pull, "anonymous_manifest_status", lambda image: 200)

    with pytest.raises(E2EError, match="image is public: pull-secret path not exercised"):
        registry_pull.check(_cfg())
    registry_pull.check(_cfg(), allow_public=True)
