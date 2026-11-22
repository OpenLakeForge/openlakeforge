from __future__ import annotations

from pathlib import Path

from test_local_prefetch import _CATALOG, _config, _ScriptedRunner, _toolkit

from olf.common.deployment.context import Profile
from olf.common.image_build import OWNER_LABEL
from olf.common.tooling.process import CommandResult
from olf.infra.deployment.local import host_images, prefetch


class _UserHasImages(_ScriptedRunner):
    """The user's Docker store already holds `owned`; every other image is absent until pulled."""

    def __init__(self, owned: set[str]) -> None:
        super().__init__()
        self._owned = owned

    def run(self, command, **kwargs):  # type: ignore[override]
        result = super().run(command, **kwargs)
        argv = self.calls[-1].argv
        if argv[1:3] == ["image", "inspect"] and argv[3] in self._owned:
            return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)
        return result


def _removed(runner: _ScriptedRunner) -> list[str]:
    return [c.argv[2] for c in runner.calls if c.argv[1:2] == ["rmi"]]


def test_destroy_removes_the_images_prefetch_pulled_and_tagged(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    runner = _UserHasImages(set())
    tools = _toolkit(runner)
    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")
    pulled = [c.argv[2] for c in runner.calls if c.argv[1:2] == ["pull"]]
    assert pulled

    host_images.prune(config, tools, env={})

    assert set(pulled) <= set(_removed(runner))
    assert prefetch.archive_reference(prefetch.polaris_images("amd64", _CATALOG)[0]) in _removed(runner)
    prune = [c.argv for c in runner.calls if c.argv[1:3] == ["image", "prune"]]
    assert prune and f"label={OWNER_LABEL}" in prune[0]


def test_an_image_the_user_had_before_is_never_removed(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    owned = _CATALOG["traefik"]
    polaris = prefetch.polaris_images("amd64", _CATALOG)[0]
    user_images = {owned, prefetch.archive_reference(polaris)}
    runner = _UserHasImages(user_images)
    tools = _toolkit(runner)
    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")

    host_images.prune(config, tools, env={})

    assert not user_images & set(_removed(runner))
    assert _removed(runner)  # the rest is still ours


def test_destroy_without_a_record_removes_only_labelled_images(tmp_path: Path) -> None:
    runner = _ScriptedRunner()

    host_images.prune(_config(tmp_path, profile=Profile.SLIM), _toolkit(runner), env={})

    assert _removed(runner) == []
