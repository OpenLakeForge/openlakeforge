"""`ToolchainManager`: the single object that turns a catalog + platform into
activated, on-disk managed executables.

Every managed tool lives under
`<home>/toolchains/<distribution-version>/<platform>/bin/<tool>`, so
multiple OpenLakeForge versions keep independent toolchains and nothing here
ever needs to touch a previous or newer version's install. A JSON receipt
next to `bin/` records exactly what was installed, so `resolve()` can skip
network and extraction entirely once a tool is already active at the
catalog's pinned version and digest.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from olf.toolchain.download import Downloader, HttpDownloader, fetch_verified
from olf.toolchain.install import install as install_archive
from olf.toolchain.platform import Platform
from olf.toolchain.spec import (
    MANAGED_TOOLS,
    ToolchainCatalogError,
    ToolSpec,
    activated_filename,
    is_valid_digest,
    load_specs,
)

DEFAULT_CATALOG_PATH = "release/component-catalog.yaml"
RECEIPT_FILENAME = "receipt.json"


def default_home() -> Path:
    override = os.environ.get("OLF_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".openlakeforge"


@dataclass(frozen=True)
class InstalledTool:
    name: str
    version: str
    sha256: str
    path: Path


@dataclass
class ToolchainManager:
    home: Path
    distribution_version: str
    platform: Platform
    specs: Mapping[str, ToolSpec]
    downloader: Downloader

    @classmethod
    def from_catalog(
        cls,
        catalog: Mapping[str, Any],
        *,
        home: Path | None = None,
        platform: Platform | None = None,
        downloader: Downloader | None = None,
    ) -> ToolchainManager:
        if not isinstance(catalog, dict):
            raise ToolchainCatalogError(f"release catalog must be a mapping, got {type(catalog).__name__}")
        distribution = catalog.get("distribution")
        if distribution is not None and not isinstance(distribution, dict):
            raise ToolchainCatalogError(
                f"release catalog's distribution must be a mapping, got {type(distribution).__name__}"
            )
        resolved_platform = platform or Platform.detect()
        distribution_version = (distribution or {}).get("version") or "unknown"
        return cls(
            home=home or default_home(),
            distribution_version=str(distribution_version),
            platform=resolved_platform,
            specs=load_specs(catalog, platform=resolved_platform),
            downloader=downloader or HttpDownloader(),
        )

    @classmethod
    def from_catalog_path(
        cls,
        catalog_path: str | Path = DEFAULT_CATALOG_PATH,
        *,
        home: Path | None = None,
        platform: Platform | None = None,
        downloader: Downloader | None = None,
    ) -> ToolchainManager:
        path = Path(catalog_path)
        if not path.is_file():
            raise ToolchainCatalogError(f"release catalog not found at {path}")
        try:
            catalog = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ToolchainCatalogError(f"release catalog at {path} is not valid YAML: {exc}") from exc
        return cls.from_catalog(catalog, home=home, platform=platform, downloader=downloader)

    @property
    def version_dir(self) -> Path:
        return self.home / "toolchains" / self.distribution_version / self.platform.key

    @property
    def bin_dir(self) -> Path:
        return self.version_dir / "bin"

    @property
    def cache_root(self) -> Path:
        return self.home / "cache"

    @property
    def receipt_path(self) -> Path:
        return self.version_dir / RECEIPT_FILENAME

    @property
    def _lock_path(self) -> Path:
        return self.version_dir / f"{RECEIPT_FILENAME}.lock"

    @contextlib.contextmanager
    def _locked_toolchain_update(self) -> Iterator[None]:
        """Serialize an entire provision-and-record sequence across
        concurrent `olf` processes sharing this `OLF_HOME` (e.g. two
        checkouts with different catalog pins, or a future parallel CI
        matrix sharing a persistent toolchain cache).

        The lock must span the installed-state check, the download/install,
        and the receipt write together - not just the receipt write. Two
        processes wanting different versions of the same tool can otherwise
        both pass the (unlocked) staleness check, race each other's
        `os.replace` onto the same activated binary path, and then each
        record their own version in the receipt: the loser's receipt entry
        can win even though the winner's binary is what's actually active,
        leaving the receipt and the on-disk binary permanently
        inconsistent. `flock` is POSIX-only, matching this project's
        darwin/linux-only `Platform` support.
        """
        # Deferred: POSIX-only, and importing it eagerly at module load
        # would fail on an unsupported platform (e.g. Windows) before
        # Platform.detect() ever gets to raise its own actionable error.
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read_receipt(self) -> dict[str, Any]:
        """A malformed receipt (missing, unparsable JSON, or valid JSON in
        the wrong shape - `null`, a list, a scalar) is treated the same as
        an absent one: the receipt is a disposable cache of what's already
        installed, not a source of truth, so `resolve()` should repair it
        by reprovisioning rather than fail the whole command on it.
        """
        if not self.receipt_path.is_file():
            return {}
        try:
            data = json.loads(self.receipt_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_receipt(self, receipt: Mapping[str, Any]) -> None:
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        # A PID-scoped tmp name, not a shared `.json.tmp`, so two processes
        # racing here can never unlink or rename over each other's staged
        # write - only the locked read-modify-write above needs exclusivity.
        tmp_path = self.receipt_path.with_name(f"{RECEIPT_FILENAME}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(dict(receipt), indent=2, sort_keys=True))
        os.replace(tmp_path, self.receipt_path)

    def installed(self, tool: str) -> InstalledTool | None:
        receipt = self._read_receipt().get(tool)
        if not isinstance(receipt, dict):
            return None
        version = receipt.get("version")
        sha256 = receipt.get("sha256")
        # A structurally valid dict can still carry a malformed field (e.g.
        # `{"version": "1.8.5", "sha256": null}`) - validate both before
        # deriving the activated path from `sha256`, or a corrupt-but-typed
        # receipt crashes every resolve() instead of being reprovisioned.
        if not isinstance(version, str) or not version or not is_valid_digest(sha256):
            return None
        path = self.bin_dir / activated_filename(tool, sha256)
        if not path.is_file() or not os.access(path, os.X_OK):
            # A cheap, existence/permission-only check, not a full re-hash
            # of the binary's content on every call - re-verifying the
            # digest here would defeat the entire point of the receipt as a
            # fast path (network/extraction-free resolution). It does
            # catch the common corruption shapes: the file being deleted,
            # or losing its executable bit (e.g. a partial backup restore
            # of a damaged OLF_HOME). If the content itself is silently
            # altered while staying executable, `resolve()` still won't
            # notice - a receipt is a cache, not a signature.
            return None
        return InstalledTool(name=tool, version=version, sha256=sha256, path=path)

    def _spec(self, tool: str) -> ToolSpec:
        if tool not in self.specs:
            raise KeyError(f"{tool!r} is not a managed tool (expected one of {MANAGED_TOOLS})")
        return self.specs[tool]

    def resolve(self, tool: str) -> Path:
        """Return the activated path for `tool`, provisioning it on first use."""
        spec = self._spec(tool)
        current = self._current_if_matching(tool, spec)
        if current is not None:
            return current

        with self._locked_toolchain_update():
            # Re-check now that the lock is held: another process may have
            # just finished installing this exact version while we waited.
            current = self._current_if_matching(tool, spec)
            if current is not None:
                return current

            archive_path = fetch_verified(self.downloader, spec, cache_root=self.cache_root)
            activated = install_archive(archive_path, spec, bin_dir=self.bin_dir)

            receipt = self._read_receipt()
            receipt[tool] = {"version": spec.version, "sha256": spec.sha256}
            self._write_receipt(receipt)
            return activated

    def _current_if_matching(self, tool: str, spec: ToolSpec) -> Path | None:
        current = self.installed(tool)
        if current is not None and current.version == spec.version and current.sha256 == spec.sha256:
            return current.path
        return None

    def ensure_all(self) -> dict[str, Path]:
        return {tool: self.resolve(tool) for tool in self.specs}

    def prune(self, *, version: str | None = None, keep_current: bool = False, remove_all: bool = False) -> list[Path]:
        """Remove installed toolchain versions under `home`, never anything
        outside it. Exactly one of `version`, `keep_current`, `remove_all`
        selects what to remove. Returns the version directories removed."""
        import shutil

        if sum(bool(x) for x in (version, keep_current, remove_all)) != 1:
            raise ValueError("prune requires exactly one of version, keep_current, or remove_all")

        home_resolved = self.home.resolve()
        toolchains_root = (self.home / "toolchains").resolve()
        if toolchains_root.parent != home_resolved:
            # `<home>/toolchains` itself resolved outside `home` (e.g. it is
            # a symlink) - refuse to trust it as prune's root at all, rather
            # than deleting whatever it points at.
            return []
        if not toolchains_root.is_dir():
            return []

        removed: list[Path] = []
        for entry in sorted(toolchains_root.iterdir()):
            if not entry.is_dir():
                continue
            resolved = entry.resolve()
            if resolved.parent != toolchains_root:
                continue  # never remove anything that escaped OLF_HOME/toolchains (e.g. a symlink)
            if version is not None:
                should_remove = entry.name == version
            elif keep_current:
                should_remove = entry.name != self.distribution_version
            else:
                should_remove = True
            if not should_remove:
                continue
            shutil.rmtree(entry)
            removed.append(entry)
        return removed
