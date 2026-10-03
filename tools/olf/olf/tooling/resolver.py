"""Injectable executable resolution.

Tool adapters must resolve executables through an `ExecutableResolver`
rather than calling `shutil.which` directly. `ManagedExecutableResolver`
(#127) is the managed-toolchain resolver this docstring originally
anticipated: managed tool names (`terraform`, `helm`, `kubectl`, `kind`) are
provisioned from `release/component-catalog.yaml` under `OLF_HOME`; every
other name (`docker`, `aws`, `az`, `uv`, `git`, ...) falls through to the
host `PATH`, unchanged from before #127.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from olf.deployment.errors import ExecutableNotFoundError, ToolchainError

if TYPE_CHECKING:
    from olf.toolchain.manager import ToolchainManager


class ExecutableResolver(Protocol):
    def resolve(self, tool: str) -> Path: ...


class PathExecutableResolver:
    """Resolves tools from PATH, with explicit overrides taking precedence."""

    def __init__(
        self,
        overrides: Mapping[str, Path] | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._overrides = dict(overrides or {})
        self._which = which

    def resolve(self, tool: str) -> Path:
        override = self._overrides.get(tool)
        if override is not None:
            return Path(override)

        found = self._which(tool)
        if found is None:
            raise ExecutableNotFoundError(tool, searched="PATH")
        return Path(found)


class ManagedExecutableResolver:
    """Resolves managed tools (#127) from a private, version-scoped
    toolchain; delegates every other tool to a `PathExecutableResolver`.

    Never falls back to an unmanaged host binary for a managed tool name —
    a provisioning failure raises `ToolchainError` rather than silently
    resolving whatever happens to be on `PATH`. `overrides` is checked
    first for every tool name (managed or not), so an explicit override
    always wins and never triggers real toolchain provisioning - the same
    contract `PathExecutableResolver` gives its callers.
    """

    def __init__(
        self,
        manager: ToolchainManager,
        *,
        fallback: ExecutableResolver,
        overrides: Mapping[str, Path] | None = None,
    ) -> None:
        self.manager = manager
        self.fallback = fallback
        self._overrides = dict(overrides or {})

    def resolve(self, tool: str) -> Path:
        override = self._overrides.get(tool)
        if override is not None:
            return Path(override)

        from olf.toolchain.spec import MANAGED_TOOLS

        if tool not in MANAGED_TOOLS:
            return self.fallback.resolve(tool)
        try:
            return self.manager.resolve(tool)
        except ExecutableNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - every toolchain failure funnels through one typed error
            raise ToolchainError(tool, reason=str(exc)) from exc


def build_resolver(
    overrides: Mapping[str, Path] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ExecutableResolver:
    """Build the resolver `Toolkit.default()` uses.

    `OLF_TOOLCHAIN_MODE` selects the strategy: `managed` (the default)
    provisions Terraform/Helm/kubectl/kind from the release catalog;
    `host` restores plain `PATH` resolution for every tool, matching
    behaviour before #127. `overrides` always wins over either strategy -
    every existing test that pins a tool path via `overrides` keeps working
    unchanged.
    """
    env = environ if environ is not None else os.environ
    fallback = PathExecutableResolver(overrides=overrides)
    mode = env.get("OLF_TOOLCHAIN_MODE", "managed")
    if mode == "host":
        return fallback
    if mode != "managed":
        # A user configuration mistake, not a provisioning failure - but
        # every olf lifecycle command's boundary only catches DeploymentError
        # (ToolchainError's base), so this must raise that family too rather
        # than a plain ValueError, or it escapes as a raw traceback.
        raise ToolchainError(
            "OLF_TOOLCHAIN_MODE", reason=f"unknown value {mode!r} (expected 'managed' or 'host')"
        )

    from olf.toolchain.manager import ToolchainManager
    from olf.toolchain.platform import UnsupportedPlatformError
    from olf.toolchain.spec import ToolchainCatalogError

    home = Path(env["OLF_HOME"]).expanduser().resolve() if env.get("OLF_HOME") else None
    if env.get("OLF_DISTRIBUTION_ROOT"):
        catalog_path = Path(env["OLF_DISTRIBUTION_ROOT"]).resolve() / "release" / "component-catalog.yaml"
    elif env.get("OPENLAKEFORGE_REPO_ROOT"):
        catalog_path = Path(env["OPENLAKEFORGE_REPO_ROOT"]).resolve() / "release" / "component-catalog.yaml"
    else:
        # No scoped environment to inherit from: this is a top-level command
        # (`olf doctor`, `olf e2e run`, a bare contract read) rather than a
        # child of `olf deploy`, which exports OLF_DISTRIBUTION_ROOT via
        # `DeploymentContext.command_env`. Resolving relative to the current
        # directory sent an installed user's default (managed) toolchain
        # lookup to `./release/component-catalog.yaml`, so every managed tool
        # failed with "release catalog not found" in their own project folder.
        from olf.distribution import DistributionError, runtime_layout

        try:
            catalog_path = runtime_layout(dict(env)).catalog_path
        except DistributionError as exc:
            raise ToolchainError("toolchain", reason=str(exc)) from exc
    try:
        manager = ToolchainManager.from_catalog_path(catalog_path, home=home)
    except (UnsupportedPlatformError, ToolchainCatalogError) as exc:
        # Either failure means every managed-tool resolve() would fail
        # identically, so fail closed here rather than on first use, through
        # the same typed error family every deployment command already
        # catches.
        raise ToolchainError("toolchain", reason=str(exc)) from exc
    return ManagedExecutableResolver(manager, fallback=fallback, overrides=overrides)
