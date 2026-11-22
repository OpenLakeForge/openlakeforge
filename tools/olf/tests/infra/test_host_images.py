from __future__ import annotations

from pathlib import Path

from test_local_prefetch import _CATALOG, _config, _ScriptedRunner, _toolkit

from olf.common import host_images
from olf.common.deployment.context import Profile
from olf.common.image_build import build_superset_image
from olf.common.tooling.process import CommandResult
from olf.infra.deployment.local import prefetch


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
    # Catalog images are pulled in the nodes; the host path remains for images a node cannot pull itself.
    prefetch.load_image_into_nodes(config, tools, prefetch.polaris_images("amd64", _CATALOG)[0], env={})
    pulled = [c.argv[2] for c in runner.calls if c.argv[1:2] == ["pull"]]
    assert pulled

    host_images.prune(config, tools, env={})

    assert set(pulled) <= set(_removed(runner))
    assert prefetch.archive_reference(prefetch.polaris_images("amd64", _CATALOG)[0]) in _removed(runner)


def test_an_image_the_user_had_before_is_never_removed(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    polaris = prefetch.polaris_images("amd64", _CATALOG)[0]
    user_images = {prefetch.archive_reference(polaris)}
    runner = _UserHasImages(user_images)
    tools = _toolkit(runner)
    prefetch.load_image_into_nodes(config, tools, polaris, env={})

    host_images.prune(config, tools, env={})

    assert not user_images & set(_removed(runner))
    assert _removed(runner)  # the rest is still ours


def test_only_the_tag_olf_built_is_removed_not_an_image_derived_from_it(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.FULL)
    runner = _ScriptedRunner()
    tools = _toolkit(runner)
    built = build_superset_image(config, tools, env={})

    host_images.prune(config, tools, env={})

    # A user's `FROM <built>` image is never named, and nothing prunes by label, which such an image inherits.
    assert _removed(runner) == [built]
    assert not any(c.argv[1:3] == ["image", "prune"] for c in runner.calls)


def test_a_failing_removal_does_not_fail_destroy(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    host_images.record(config, "a:1")

    class _Failing(_ScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            super().run(command, **kwargs)
            raise RuntimeError("daemon gone")

    host_images.prune(config, _toolkit(_Failing()), env={})

    assert not (config.paths.work_root / "olf-host-images.txt").exists()


def test_destroy_without_a_record_removes_nothing(tmp_path: Path) -> None:
    runner = _ScriptedRunner()

    host_images.prune(_config(tmp_path, profile=Profile.SLIM), _toolkit(runner), env={})

    assert _removed(runner) == []
