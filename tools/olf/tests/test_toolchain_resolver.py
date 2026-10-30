from __future__ import annotations

from pathlib import Path

import pytest

from olf.deployment.errors import ExecutableNotFoundError, ToolchainError
from olf.tooling.resolver import (
    ManagedExecutableResolver,
    PathExecutableResolver,
    build_resolver,
)


def test_path_resolver_still_resolves_from_path() -> None:
    resolver = PathExecutableResolver(which=lambda name: f"/usr/bin/{name}" if name == "docker" else None)
    assert resolver.resolve("docker") == Path("/usr/bin/docker")
    with pytest.raises(ExecutableNotFoundError):
        resolver.resolve("terraform")


def test_path_resolver_overrides_take_precedence_over_which() -> None:
    resolver = PathExecutableResolver(
        overrides={"terraform": Path("/opt/pinned/terraform")}, which=lambda _name: "/usr/bin/terraform"
    )
    assert resolver.resolve("terraform") == Path("/opt/pinned/terraform")


class _FakeManager:
    def __init__(self, paths: dict[str, Path] | None = None, *, fail_with: Exception | None = None) -> None:
        self._paths = paths or {}
        self._fail_with = fail_with
        self.calls: list[str] = []

    def resolve(self, tool: str) -> Path:
        self.calls.append(tool)
        if self._fail_with is not None:
            raise self._fail_with
        return self._paths[tool]


def test_managed_resolver_delegates_managed_tools_to_the_manager() -> None:
    manager = _FakeManager({"terraform": Path("/home/.openlakeforge/toolchains/x/bin/terraform")})
    resolver = ManagedExecutableResolver(manager, fallback=PathExecutableResolver(which=lambda _n: None))

    result = resolver.resolve("terraform")

    assert result == Path("/home/.openlakeforge/toolchains/x/bin/terraform")
    assert manager.calls == ["terraform"]


def test_managed_resolver_falls_through_unmanaged_tools_to_path() -> None:
    manager = _FakeManager()
    resolver = ManagedExecutableResolver(
        manager, fallback=PathExecutableResolver(which=lambda name: f"/usr/bin/{name}" if name == "docker" else None)
    )

    result = resolver.resolve("docker")

    assert result == Path("/usr/bin/docker")
    assert manager.calls == []  # docker is not a managed tool; the manager is never consulted


@pytest.mark.parametrize("tool", ["terraform", "helm", "kubectl", "kind"])
def test_managed_resolver_never_falls_back_to_path_for_a_managed_tool_on_failure(tool: str) -> None:
    manager = _FakeManager(fail_with=RuntimeError("network unreachable"))
    fallback_calls: list[str] = []

    class _RecordingFallback:
        def resolve(self, name: str) -> Path:
            fallback_calls.append(name)
            return Path(f"/usr/bin/{name}")

    resolver = ManagedExecutableResolver(manager, fallback=_RecordingFallback())

    with pytest.raises(ToolchainError, match=tool):
        resolver.resolve(tool)

    assert fallback_calls == []  # a managed-tool failure must never silently resolve a host binary


def test_managed_resolver_wraps_manager_failures_with_an_actionable_escape_hatch() -> None:
    manager = _FakeManager(fail_with=RuntimeError("digest mismatch"))
    resolver = ManagedExecutableResolver(manager, fallback=PathExecutableResolver(which=lambda _n: None))

    with pytest.raises(ToolchainError, match="OLF_TOOLCHAIN_MODE=host"):
        resolver.resolve("kind")


def test_managed_resolver_propagates_executable_not_found_unwrapped() -> None:
    """A plain PATH miss for a managed tool should surface the standard
    ExecutableNotFoundError message, not be double-wrapped."""

    class _NotFoundManager:
        def resolve(self, tool: str) -> Path:
            raise ExecutableNotFoundError(tool, searched="OLF_HOME")

    resolver = ManagedExecutableResolver(_NotFoundManager(), fallback=PathExecutableResolver(which=lambda _n: None))

    with pytest.raises(ExecutableNotFoundError):
        resolver.resolve("terraform")


def test_build_resolver_host_mode_returns_a_plain_path_resolver(tmp_path: Path) -> None:
    resolver = build_resolver(environ={"OLF_TOOLCHAIN_MODE": "host"})
    assert isinstance(resolver, PathExecutableResolver)
    assert not isinstance(resolver, ManagedExecutableResolver)


def test_build_resolver_rejects_an_unknown_mode() -> None:
    """A misspelled OLF_TOOLCHAIN_MODE is a user configuration mistake, not
    a provisioning failure - but every olf lifecycle command's boundary
    only catches DeploymentError (ToolchainError's base), so this must
    raise that family, not a plain ValueError, or it escapes uncaught."""
    with pytest.raises(ToolchainError, match="OLF_TOOLCHAIN_MODE"):
        build_resolver(environ={"OLF_TOOLCHAIN_MODE": "bogus"})


def test_build_resolver_managed_mode_wires_a_managed_resolver_against_the_real_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("olf.config.repo_root", lambda: Path("."))
    resolver = build_resolver(environ={"OLF_TOOLCHAIN_MODE": "managed", "OLF_HOME": str(tmp_path)})

    assert isinstance(resolver, ManagedExecutableResolver)
    assert resolver.manager.home == tmp_path
    assert set(resolver.manager.specs) == {"terraform", "helm", "kubectl", "kind"}


def test_build_resolver_overrides_win_over_managed_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("olf.config.repo_root", lambda: Path("."))
    pinned = Path("/opt/pinned/terraform")
    resolver = build_resolver(
        overrides={"terraform": pinned}, environ={"OLF_TOOLCHAIN_MODE": "managed", "OLF_HOME": str(tmp_path)}
    )

    assert resolver.resolve("terraform") == pinned


def test_build_resolver_managed_mode_fails_closed_on_a_missing_catalog(tmp_path: Path) -> None:
    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    with pytest.raises(ToolchainError):
        build_resolver(
            environ={
                "OLF_TOOLCHAIN_MODE": "managed",
                "OLF_HOME": str(tmp_path / "home"),
                "OLF_DISTRIBUTION_ROOT": str(empty_repo),
            }
        )


def test_build_resolver_managed_mode_resolves_the_catalog_outside_the_current_directory(tmp_path: Path) -> None:
    """With no scoped environment to inherit (a top-level `olf doctor`/
    `olf e2e run`, not a child of `olf deploy`), the catalog must come from
    the active runtime layout - not from the current working directory.

    Regression test: resolving relative to the cwd meant an installed user
    running any managed-toolchain command from their own (catalog-free)
    project folder got "release catalog not found at
    ./release/component-catalog.yaml", which broke the default toolchain
    mode for every quick-start command.
    """
    elsewhere = tmp_path / "some-user-project"
    elsewhere.mkdir()

    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(elsewhere)
        resolver = build_resolver(
            environ={"OLF_TOOLCHAIN_MODE": "managed", "OLF_HOME": str(tmp_path / "home")}
        )

    assert isinstance(resolver, ManagedExecutableResolver)
    assert set(resolver.manager.specs) == {"terraform", "helm", "kubectl", "kind"}
