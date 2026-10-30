"""Fetching a tool archive into the content-addressed download cache.

`Downloader` is a narrow protocol so tests substitute an in-memory fake
instead of hitting the network; `HttpDownloader` is the real implementation,
built on the `requests` dependency `olf` already carries.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Protocol

from olf.toolchain.errors import ToolchainDownloadError, ToolchainVerificationError
from olf.toolchain.spec import ToolSpec


class Downloader(Protocol):
    def fetch(self, url: str, *, destination: Path) -> None:
        """Write the full content of `url` to `destination`. Must not leave a
        partial file at `destination` on failure."""
        ...


class HttpDownloader:
    def __init__(self, *, timeout_seconds: float = 120.0, chunk_size: int = 1 << 16) -> None:
        self._timeout_seconds = timeout_seconds
        self._chunk_size = chunk_size

    def fetch(self, url: str, *, destination: Path) -> None:
        import requests

        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=destination.parent, prefix=".download-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                try:
                    response = requests.get(url, stream=True, timeout=self._timeout_seconds)
                    response.raise_for_status()
                    for chunk in response.iter_content(chunk_size=self._chunk_size):
                        if chunk:
                            handle.write(chunk)
                except requests.RequestException as exc:
                    raise ToolchainDownloadError(f"failed to download {url}: {exc}") from exc
            os.replace(tmp_path, destination)
        finally:
            tmp_path.unlink(missing_ok=True)


def cache_path(cache_root: Path, spec: ToolSpec) -> Path:
    digest = spec.sha256.removeprefix("sha256:")
    return cache_root / "downloads" / digest


def fetch_verified(downloader: Downloader, spec: ToolSpec, *, cache_root: Path) -> Path:
    """Return a cached, digest-verified copy of `spec`'s archive, downloading
    it first if it is not already cached (or if a stale cache entry no longer
    matches its own name).

    Locked per digest, independent of any caller-side lock: this cache is
    content-addressed and intentionally shared across every
    `ToolchainManager` (and thus every distribution version) pointed at the
    same `OLF_HOME`, so a manager's own per-version lock does not protect
    it. Without a lock scoped here, two managers that happen to pin the
    same tool digest and concurrently find a stale/corrupted cache entry
    could both pass the staleness check and then race each other's
    `unlink()` - the loser gets `FileNotFoundError` instead of the cache
    being repaired.
    """
    # Deferred: POSIX-only, and importing it eagerly at module load would
    # fail on an unsupported platform (e.g. Windows) before Platform.detect()
    # ever gets to raise its own actionable error - manager.py imports this
    # module unconditionally.
    import fcntl

    destination = cache_path(cache_root, spec)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f"{destination.name}.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return _fetch_verified_locked(downloader, spec, destination)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _fetch_verified_locked(downloader: Downloader, spec: ToolSpec, destination: Path) -> Path:
    if destination.is_file() and _sha256(destination) == spec.sha256.removeprefix("sha256:"):
        return destination
    if destination.exists():
        destination.unlink(missing_ok=True)

    downloader.fetch(spec.url, destination=destination)
    actual = _sha256(destination)
    expected = spec.sha256.removeprefix("sha256:")
    if actual != expected:
        destination.unlink(missing_ok=True)
        raise ToolchainVerificationError(spec.name, expected=expected, actual=actual)
    return destination


def _sha256(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
