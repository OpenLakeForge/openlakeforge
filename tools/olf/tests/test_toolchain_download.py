from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from olf.toolchain.download import cache_path, fetch_verified
from olf.toolchain.errors import ToolchainVerificationError
from olf.toolchain.platform import Platform
from olf.toolchain.spec import ToolSpec

_CONTENT = b"fake-tool-binary-content"
_DIGEST = hashlib.sha256(_CONTENT).hexdigest()


def _spec(sha256: str = f"sha256:{_DIGEST}") -> ToolSpec:
    return ToolSpec(
        name="kind",
        version="1.0.0",
        platform=Platform(os="linux", arch="amd64"),
        sha256=sha256,
        url="https://example.invalid/kind",
        archive="raw",
    )


class _FakeDownloader:
    def __init__(self, content: bytes = _CONTENT) -> None:
        self.content = content
        self.calls: list[str] = []

    def fetch(self, url: str, *, destination: Path) -> None:
        self.calls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.content)


class _FailingDownloader:
    def fetch(self, url: str, *, destination: Path) -> None:
        raise RuntimeError("network is unreachable")


def test_fetch_verified_downloads_and_verifies(tmp_path: Path) -> None:
    downloader = _FakeDownloader()
    spec = _spec()

    result = fetch_verified(downloader, spec, cache_root=tmp_path)

    assert downloader.calls == [spec.url]
    assert result == cache_path(tmp_path, spec)
    assert result.read_bytes() == _CONTENT


def test_fetch_verified_reuses_a_valid_cache_entry_without_downloading(tmp_path: Path) -> None:
    downloader = _FakeDownloader()
    spec = _spec()
    fetch_verified(downloader, spec, cache_root=tmp_path)

    fetch_verified(downloader, spec, cache_root=tmp_path)

    assert downloader.calls == [spec.url]  # only the first call touched the network


def test_fetch_verified_rejects_a_digest_mismatch_and_leaves_no_file(tmp_path: Path) -> None:
    downloader = _FakeDownloader(content=b"wrong content entirely")
    spec = _spec()

    with pytest.raises(ToolchainVerificationError):
        fetch_verified(downloader, spec, cache_root=tmp_path)

    assert not cache_path(tmp_path, spec).exists()


def test_fetch_verified_redownloads_a_stale_cache_entry(tmp_path: Path) -> None:
    spec = _spec()
    destination = cache_path(tmp_path, spec)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"corrupted leftover from a prior run")
    downloader = _FakeDownloader()

    result = fetch_verified(downloader, spec, cache_root=tmp_path)

    assert downloader.calls == [spec.url]
    assert result.read_bytes() == _CONTENT


def test_fetch_verified_propagates_download_failures(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unreachable"):
        fetch_verified(_FailingDownloader(), _spec(), cache_root=tmp_path)


def test_fetch_verified_serializes_concurrent_stale_cache_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two managers sharing a digest (e.g. two distribution versions pinning
    the same tool version) that both find a corrupted cache entry at the
    same time must not race each other's unlink - the loser must not crash
    with FileNotFoundError, and the cache must end up correctly repaired.

    A real `Path.unlink()` is fast enough that plain GIL scheduling rarely
    reproduces the race deterministically, so a small sleep is injected
    around every unlink of the contested `destination` path to widen the
    window - real time, not an artificial synchronization point, so a
    correctly-locked implementation (whose second caller never even reaches
    its own unlink because the earlier one already repaired the cache
    under the lock) is unaffected rather than deadlocking on a barrier that
    only one side reaches.
    """
    import threading
    import time

    spec = _spec()
    destination = cache_path(tmp_path, spec)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"corrupted leftover from a prior run")

    real_unlink = Path.unlink

    def _slow_unlink(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if self == destination:
            time.sleep(0.05)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _slow_unlink)

    start_barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[Path] = []
    results_lock = threading.Lock()

    def _fetch() -> None:
        start_barrier.wait(timeout=5)
        try:
            result = fetch_verified(_FakeDownloader(), spec, cache_root=tmp_path)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)
        else:
            with results_lock:
                results.append(result)

    threads = [threading.Thread(target=_fetch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert len(results) == 2
    assert results[0] == results[1] == destination
    assert destination.read_bytes() == _CONTENT
