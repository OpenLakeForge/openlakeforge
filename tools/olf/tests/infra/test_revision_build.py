from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from olf.infra import project_revision
from olf.infra.artifact_store import FilesystemRevisionStore
from olf.infra.deployment import revision_build

_IMAGE = "ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "a" * 64


def test_publish_project_revision_publishes_a_verifiable_revision(external_project: Path, tmp_path: Path) -> None:
    store = FilesystemRevisionStore(tmp_path / "store")

    manifest = revision_build.publish_project_revision(
        str(external_project), image=_IMAGE, open_store=lambda _root: nullcontext(store)
    )

    assert manifest.project_code_image == _IMAGE
    verified = project_revision.verify(store, manifest.revision, running_distribution_version=None)
    assert verified == manifest


def test_build_stage_revision_pins_the_image_the_provider_built(
    external_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushes: list[bool] = []

    def build_project_image(*, push: bool) -> str:
        pushes.append(push)
        return _IMAGE

    seen: dict[str, Any] = {}
    sentinel = object()

    def publish(project: str, **kwargs: Any) -> object:
        seen.update(project=project, **kwargs)
        return sentinel

    monkeypatch.setattr(revision_build, "publish_project_revision", publish)
    provider: Any = SimpleNamespace(build_project_image=build_project_image)
    context: Any = SimpleNamespace()

    result = revision_build.build_stage_revision(provider, context, str(external_project))

    assert result is sentinel
    assert pushes == [True]
    assert seen["image"] == _IMAGE
    assert seen["project"] == str(external_project)


def test_build_stage_revision_can_skip_the_push_for_the_local_loop(
    external_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_image = f"{project_revision.LOCAL_IMAGE_REPOSITORY}@sha256:" + "c" * 64
    pushes: list[bool] = []
    seen: dict[str, Any] = {}

    def build_project_image(*, push: bool) -> str:
        pushes.append(push)
        return local_image

    monkeypatch.setattr(revision_build, "publish_project_revision", lambda project, **kwargs: seen.update(kwargs))
    provider: Any = SimpleNamespace(build_project_image=build_project_image)

    revision_build.build_stage_revision(provider, SimpleNamespace(), str(external_project), push=False)  # type: ignore[arg-type]

    assert pushes == [False]
    assert seen["image"] == local_image


def test_activate_stage_project_activates_the_revision_it_built(
    external_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.infra.deployment import activation

    store = object()
    opened: list[Path] = []
    activated: dict[str, Any] = {}
    sentinel = object()
    provider: Any = SimpleNamespace(context=SimpleNamespace())

    monkeypatch.setattr(
        revision_build, "build_stage_revision", lambda *_args: SimpleNamespace(revision="sha256:" + "b" * 64)
    )

    def open_store(_provider: object, _context: object, root: Path) -> Any:
        opened.append(root)
        return nullcontext(store)

    def deploy_revision(received: object, **kwargs: Any) -> object:
        activated.update(provider=received, **kwargs)
        return sentinel

    monkeypatch.setattr(revision_build, "provider_revision_store", open_store)
    monkeypatch.setattr(activation, "deploy_revision", deploy_revision)

    result = revision_build.activate_stage_project(
        provider, str(external_project), profile_name="demo", pull_secret="ghcr-pull"
    )

    assert result is sentinel
    assert opened == [external_project]
    assert activated == {
        "provider": provider,
        "revision": "sha256:" + "b" * 64,
        "store": store,
        "profile_name": "demo",
        "pull_secret": "ghcr-pull",
    }
