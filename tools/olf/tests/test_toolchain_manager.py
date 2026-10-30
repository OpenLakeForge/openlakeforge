from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from olf.toolchain.manager import ToolchainManager
from olf.toolchain.platform import Platform
from olf.toolchain.spec import ToolchainCatalogError, activated_filename, load_specs

_PLATFORM = Platform(os="linux", arch="amd64")
_TOOLS = ("terraform", "helm", "kubectl", "kind")


def _archive_bytes(tool: str, spec) -> bytes:  # noqa: ANN001
    """Build a real zip/tar.gz/raw payload matching `spec`'s own archive
    layout, so the fake downloader exercises the same extraction code path
    a real download would.
    """
    # Version is embedded in the payload, matching a real release: a
    # version bump always changes the binary's bytes/digest too, so a fake
    # digest collision across versions (which would mask a version-bump
    # re-provisioning bug behind cache reuse) can't happen here.
    payload = f"pretend {tool} binary v{spec.version}".encode()
    if spec.archive == "raw":
        return payload
    # Byte-for-byte reproducible, because the caller digests one call's output
    # and then serves another's: `_catalog_and_digests` pins the digest, and
    # the fake downloader rebuilds the archive when the manager asks for it. A
    # container format that stamps the current time makes those two disagree
    # whenever they land in different seconds, which failed as a digest
    # mismatch on whichever tool happened to straddle the boundary.
    if spec.archive == "zip":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            # ZipInfo from a bare name would take `time.localtime()`.
            zf.writestr(zipfile.ZipInfo(filename=spec.member, date_time=(1980, 1, 1, 0, 0, 0)), payload)
        return buffer.getvalue()
    if spec.archive == "tar.gz":
        tar = io.BytesIO()
        with tarfile.open(fileobj=tar, mode="w") as tf:
            info = tarfile.TarInfo(name=spec.member)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        buffer = io.BytesIO()
        # gzip writes the current mtime into its header unless told otherwise,
        # and `tarfile`'s own "w:gz" mode gives no way to set it.
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
            gz.write(tar.getvalue())
        return buffer.getvalue()
    raise AssertionError(spec.archive)


def _catalog_and_digests(version: str = "1.0.0") -> tuple[dict, dict[str, str]]:
    """A catalog with placeholder digests, plus the real digests for the
    fake archives that will actually be served - computed by round-tripping
    through the real `load_specs` so archive kind/member always matches
    what `ToolchainManager` will really request.
    """
    placeholder = "sha256:" + "0" * 64
    draft_catalog = {
        "components": {
            "toolchain": {tool: {"version": version, "platforms": {_PLATFORM.key: placeholder}} for tool in _TOOLS}
        }
    }
    specs = load_specs(draft_catalog, platform=_PLATFORM)
    digests = {tool: "sha256:" + hashlib.sha256(_archive_bytes(tool, spec)).hexdigest() for tool, spec in specs.items()}
    catalog = {
        "distribution": {"version": "0.1.0-alpha.1"},
        "components": {
            "toolchain": {
                tool: {"version": version, "platforms": {_PLATFORM.key: digests[tool]}} for tool in _TOOLS
            }
        },
    }
    return catalog, digests


class _FakeDownloader:
    def __init__(self, specs: dict) -> None:
        self._specs = specs
        self.calls: list[str] = []

    def fetch(self, url: str, *, destination: Path) -> None:
        self.calls.append(url)
        tool = next(name for name, spec in self._specs.items() if spec.url == url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_archive_bytes(tool, self._specs[tool]))


def _manager(
    tmp_path: Path, *, catalog: dict | None = None, home: Path | None = None
) -> tuple[ToolchainManager, _FakeDownloader]:
    resolved_catalog = catalog or _catalog_and_digests()[0]
    specs = load_specs(resolved_catalog, platform=_PLATFORM)
    downloader = _FakeDownloader(specs)
    manager = ToolchainManager(
        home=home or (tmp_path / "home"),
        distribution_version=str(resolved_catalog["distribution"]["version"]),
        platform=_PLATFORM,
        specs=specs,
        downloader=downloader,
    )
    return manager, downloader


def test_resolve_provisions_a_managed_tool_on_first_use(tmp_path: Path) -> None:
    manager, downloader = _manager(tmp_path)

    path = manager.resolve("kind")

    assert path.is_file()
    assert path.read_bytes() == b"pretend kind binary v1.0.0"
    assert len(downloader.calls) == 1


def test_resolve_is_idempotent_and_touches_the_network_only_once(tmp_path: Path) -> None:
    manager, downloader = _manager(tmp_path)

    first = manager.resolve("terraform")
    second = manager.resolve("terraform")

    assert first == second
    assert first.read_bytes() == b"pretend terraform binary v1.0.0"
    assert len(downloader.calls) == 1


def test_resolve_writes_a_receipt_recording_version_and_digest(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)

    manager.resolve("helm")

    receipt = json.loads(manager.receipt_path.read_text())
    assert receipt["helm"]["version"] == manager.specs["helm"].version
    assert receipt["helm"]["sha256"] == manager.specs["helm"].sha256


def test_resolve_reprovisions_when_the_catalog_bumps_the_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager_v1, _ = _manager(tmp_path, home=home)
    manager_v1.resolve("kubectl")

    catalog_v2, _ = _catalog_and_digests(version="2.0.0")
    manager_v2, downloader_v2 = _manager(tmp_path, catalog=catalog_v2, home=home)
    # Same distribution version (receipts are per version_dir) but a bumped
    # tool version - the receipt no longer matches the catalog's spec.
    manager_v2 = ToolchainManager(
        home=home,
        distribution_version=manager_v1.distribution_version,
        platform=_PLATFORM,
        specs=manager_v2.specs,
        downloader=downloader_v2,
    )

    manager_v2.resolve("kubectl")

    assert len(downloader_v2.calls) == 1  # version differs from the receipt, so it re-provisions


def test_resolve_raises_for_an_unmanaged_tool_name(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    with pytest.raises(KeyError):
        manager.resolve("docker")


def test_ensure_all_provisions_every_managed_tool(tmp_path: Path) -> None:
    manager, downloader = _manager(tmp_path)

    paths = manager.ensure_all()

    assert set(paths) == set(_TOOLS)
    assert len(downloader.calls) == len(_TOOLS)
    assert all(path.is_file() for path in paths.values())


def test_two_distribution_versions_keep_independent_toolchains(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog_a, _ = _catalog_and_digests()
    catalog_a["distribution"]["version"] = "0.1.0-alpha.1"
    catalog_b, _ = _catalog_and_digests()
    catalog_b["distribution"]["version"] = "0.2.0-alpha.1"

    manager_a, _ = _manager(tmp_path, catalog=catalog_a, home=home)
    manager_b, _ = _manager(tmp_path, catalog=catalog_b, home=home)

    path_a = manager_a.resolve("kind")
    path_b = manager_b.resolve("kind")

    assert path_a != path_b
    assert path_a.is_file()
    assert path_b.is_file()
    assert manager_a.version_dir != manager_b.version_dir


def test_from_catalog_path_raises_a_typed_error_for_a_missing_catalog(tmp_path: Path) -> None:
    with pytest.raises(ToolchainCatalogError):
        ToolchainManager.from_catalog_path(tmp_path / "missing.yaml", home=tmp_path / "home", platform=_PLATFORM)


def test_prune_requires_exactly_one_selector(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.prune()
    with pytest.raises(ValueError):
        manager.prune(version="1.0.0", remove_all=True)


def test_prune_all_removes_every_installed_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog_a, _ = _catalog_and_digests()
    catalog_a["distribution"]["version"] = "0.1.0-alpha.1"
    catalog_b, _ = _catalog_and_digests()
    catalog_b["distribution"]["version"] = "0.2.0-alpha.1"
    manager_a, _ = _manager(tmp_path, catalog=catalog_a, home=home)
    manager_b, _ = _manager(tmp_path, catalog=catalog_b, home=home)
    manager_a.resolve("kind")
    manager_b.resolve("kind")

    removed = manager_a.prune(remove_all=True)

    assert sorted(p.name for p in removed) == ["0.1.0-alpha.1", "0.2.0-alpha.1"]
    assert not manager_a.version_dir.exists()
    assert not manager_b.version_dir.exists()


def test_prune_keep_current_preserves_only_the_active_distribution(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog_a, _ = _catalog_and_digests()
    catalog_a["distribution"]["version"] = "0.1.0-alpha.1"
    catalog_b, _ = _catalog_and_digests()
    catalog_b["distribution"]["version"] = "0.2.0-alpha.1"
    manager_a, _ = _manager(tmp_path, catalog=catalog_a, home=home)
    manager_b, _ = _manager(tmp_path, catalog=catalog_b, home=home)
    manager_a.resolve("kind")
    manager_b.resolve("kind")

    removed = manager_b.prune(keep_current=True)

    assert [p.name for p in removed] == ["0.1.0-alpha.1"]
    assert not manager_a.version_dir.exists()
    assert manager_b.version_dir.exists()


def test_prune_never_removes_anything_outside_olf_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "definitely-not-olf-home"
    outside.mkdir()
    (outside / "marker.txt").write_text("do not touch")
    (home / "toolchains").mkdir(parents=True)
    escape_link = home / "toolchains" / "escaped-version"
    escape_link.symlink_to(outside)

    manager, _ = _manager(tmp_path, home=home)

    manager.prune(remove_all=True)

    assert outside.is_dir()
    assert (outside / "marker.txt").is_file()


def test_concurrent_resolves_of_different_tools_do_not_clobber_each_others_receipt(tmp_path: Path) -> None:
    """Two `olf` processes (simulated here as threads sharing one manager,
    which is the realistic shape of the race - independent `ToolchainManager`
    instances pointed at the same `OLF_HOME`) provisioning different tools at
    the same time must not lose either receipt entry, and must never crash
    on the temp-file rename `_write_receipt` performs.
    """
    import threading

    manager, _ = _manager(tmp_path)
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _resolve(tool: str) -> None:
        barrier.wait(timeout=5)
        try:
            manager.resolve(tool)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_resolve, args=(tool,)) for tool in ("terraform", "helm")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    receipt = json.loads(manager.receipt_path.read_text())
    assert set(receipt) == {"terraform", "helm"}


class _TimingDownloader:
    """Wraps a real fake downloader but records the wall-clock interval
    each `fetch()` call spans, in a shared list guarded by a lock. A short
    sleep widens the window so two racing threads are very likely to
    overlap unless something outside `fetch()` itself already serializes
    them - i.e. this observes whether the *caller's* critical section
    (installed-state check through receipt write) is exclusive, not just
    whether `fetch()` calls happen to interleave.
    """

    def __init__(self, inner: _FakeDownloader, intervals: list, lock) -> None:  # noqa: ANN001
        self._inner = inner
        self._intervals = intervals
        self._lock = lock
        self.calls = inner.calls

    def fetch(self, url: str, *, destination: Path) -> None:
        import time

        start = time.monotonic()
        time.sleep(0.05)
        self._inner.fetch(url, destination=destination)
        end = time.monotonic()
        with self._lock:
            self._intervals.append((start, end))


def _intervals_overlap(intervals: list[tuple[float, float]]) -> bool:
    ordered = sorted(intervals)
    return any(a_end > b_start for (_, a_end), (b_start, _) in zip(ordered, ordered[1:], strict=False))


def test_concurrent_resolves_of_different_versions_never_leave_a_binary_receipt_mismatch(tmp_path: Path) -> None:
    """Two `olf` processes pinning different versions of the same tool
    (e.g. two checkouts sharing OLF_HOME) must never end up with a receipt
    that claims one version while the activated binary is actually the
    other - each racing writer must see the other's outcome atomically,
    which requires their entire provision-and-record sequences to never
    overlap in wall-clock time.
    """
    import threading

    home = tmp_path / "home"
    catalog_a, _ = _catalog_and_digests(version="1.0.0")
    catalog_b, _ = _catalog_and_digests(version="2.0.0")
    manager_a, downloader_a = _manager(tmp_path, catalog=catalog_a, home=home)
    manager_b, downloader_b = _manager(tmp_path, catalog=catalog_b, home=home)

    intervals: list[tuple[float, float]] = []
    intervals_lock = threading.Lock()
    manager_a.downloader = _TimingDownloader(downloader_a, intervals, intervals_lock)
    manager_b.downloader = _TimingDownloader(downloader_b, intervals, intervals_lock)

    errors: list[BaseException] = []
    start_barrier = threading.Barrier(2)

    def _resolve(manager: ToolchainManager) -> None:
        start_barrier.wait(timeout=5)
        try:
            manager.resolve("kubectl")
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_resolve, args=(m,)) for m in (manager_a, manager_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert len(intervals) == 2  # both threads actually downloaded, so overlap would have been possible
    assert not _intervals_overlap(intervals), (
        "the two provisioning sequences overlapped - the lock did not serialize them"
    )

    receipt = json.loads(manager_a.receipt_path.read_text())
    recorded_version = receipt["kubectl"]["version"]
    recorded_sha256 = receipt["kubectl"]["sha256"]
    activated_path = manager_a.bin_dir / activated_filename("kubectl", recorded_sha256)
    expected_bytes = f"pretend kubectl binary v{recorded_version}".encode()
    assert activated_path.read_bytes() == expected_bytes, (
        f"receipt says {recorded_version!r} but the digest-named activated binary does not match it"
    )


def test_a_resolved_path_survives_a_later_resolve_of_a_different_pin(tmp_path: Path) -> None:
    """The core TOCTOU scenario: checkout A resolves a path, then (after A's
    `resolve()` call has already returned) checkout B resolves a different
    pin of the same tool under the same OLF_HOME/version_dir. A's path must
    still contain exactly what A resolved - a content-addressed path can
    never be mutated by someone else's install."""
    home = tmp_path / "home"
    catalog_a, _ = _catalog_and_digests(version="1.0.0")
    catalog_b, _ = _catalog_and_digests(version="2.0.0")
    manager_a, _ = _manager(tmp_path, catalog=catalog_a, home=home)
    manager_b, _ = _manager(tmp_path, catalog=catalog_b, home=home)

    path_a = manager_a.resolve("terraform")
    content_when_a_resolved = path_a.read_bytes()

    manager_b.resolve("terraform")  # a different pin, well after A's call returned

    assert path_a.read_bytes() == content_when_a_resolved
    assert path_a != manager_b.installed("terraform").path


def test_prune_refuses_a_symlinked_toolchains_root(tmp_path: Path) -> None:
    """If `<OLF_HOME>/toolchains` itself is a symlink to an external
    directory, `prune` must not trust that directory as its root at all -
    otherwise every real child of the external directory satisfies the
    per-entry `resolved.parent == toolchains_root` check and gets deleted,
    defeating the "never remove anything outside OLF_HOME" guarantee.
    """
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "definitely-not-olf-home"
    outside.mkdir()
    (outside / "some-version").mkdir()
    (outside / "some-version" / "marker.txt").write_text("do not touch")
    (home / "toolchains").symlink_to(outside)

    manager, _ = _manager(tmp_path, home=home)

    removed = manager.prune(remove_all=True)

    assert removed == []
    assert (outside / "some-version").is_dir()
    assert (outside / "some-version" / "marker.txt").is_file()


@pytest.mark.parametrize("malformed_document", [None, [], 42, "not-a-dict"])
def test_installed_treats_a_malformed_receipt_document_as_absent(tmp_path: Path, malformed_document: object) -> None:
    """A receipt.json containing valid JSON in the wrong shape (null, a
    list, a scalar) must be treated the same as an absent receipt - it's a
    disposable cache of what's installed, not a source of truth, so
    resolve() should repair it by reprovisioning rather than crash the
    whole command."""
    manager, _ = _manager(tmp_path)
    manager.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    manager.receipt_path.write_text(json.dumps(malformed_document))

    assert manager.installed("terraform") is None


@pytest.mark.parametrize("malformed_entry", [None, [], 42, "not-a-dict"])
def test_installed_treats_a_malformed_per_tool_entry_as_absent(tmp_path: Path, malformed_entry: object) -> None:
    manager, _ = _manager(tmp_path)
    manager.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    manager.receipt_path.write_text(json.dumps({"terraform": malformed_entry}))

    assert manager.installed("terraform") is None


def test_resolve_repairs_a_malformed_receipt_instead_of_crashing(tmp_path: Path) -> None:
    manager, downloader = _manager(tmp_path)
    manager.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    manager.receipt_path.write_text("null")

    path = manager.resolve("kind")

    assert path.is_file()
    assert len(downloader.calls) == 1


@pytest.mark.parametrize(
    "malformed_fields",
    [
        {"version": "1.8.5", "sha256": None},
        {"version": "1.8.5", "sha256": 42},
        {"version": "1.8.5", "sha256": "not-a-digest"},
        {"version": None, "sha256": "sha256:" + "a" * 64},
        {"version": 42, "sha256": "sha256:" + "a" * 64},
        {"version": "", "sha256": "sha256:" + "a" * 64},
    ],
)
def test_installed_treats_a_receipt_with_a_malformed_field_as_absent(
    tmp_path: Path, malformed_fields: dict
) -> None:
    """A structurally valid dict entry can still carry a malformed field
    (e.g. `sha256: null`) - activated_filename() would otherwise crash on
    a None/non-digest value instead of this disposable cache entry being
    treated as absent and reprovisioned."""
    manager, _ = _manager(tmp_path)
    manager.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    manager.receipt_path.write_text(json.dumps({"terraform": malformed_fields}))

    assert manager.installed("terraform") is None


def test_resolve_repairs_a_receipt_with_a_null_sha256_instead_of_crashing(tmp_path: Path) -> None:
    manager, downloader = _manager(tmp_path)
    manager.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    manager.receipt_path.write_text(json.dumps({"kind": {"version": "0.26.0", "sha256": None}}))

    path = manager.resolve("kind")

    assert path.is_file()
    assert len(downloader.calls) == 1


def test_installed_refuses_an_activated_file_that_lost_its_executable_bit(tmp_path: Path) -> None:
    """A digest-named activated file with a valid receipt but no execute
    permission (e.g. a partial restore of a damaged OLF_HOME) must not be
    trusted as installed - the next command would otherwise fail with a
    permission error instead of resolve() repairing it."""
    manager, downloader = _manager(tmp_path)
    path = manager.resolve("kind")
    assert manager.installed("kind") is not None

    path.chmod(0o644)  # drop the executable bit, keep the content intact

    assert manager.installed("kind") is None


def test_resolve_reinstalls_from_the_verified_cache_when_the_executable_bit_is_lost(tmp_path: Path) -> None:
    """The repair path must reuse the already-verified download cache
    entry rather than re-downloading - resolve() should not need the
    network just because a local file lost a permission bit."""
    manager, downloader = _manager(tmp_path)
    first_path = manager.resolve("kind")
    assert len(downloader.calls) == 1

    first_path.chmod(0o644)

    second_path = manager.resolve("kind")

    assert second_path == first_path
    assert os.access(second_path, os.X_OK)
    assert len(downloader.calls) == 1  # reused the cached archive, no re-download


def test_from_catalog_path_raises_a_typed_error_for_invalid_yaml(tmp_path: Path) -> None:
    catalog_path = tmp_path / "component-catalog.yaml"
    catalog_path.write_text("distribution: [unterminated\n  - broken")

    with pytest.raises(ToolchainCatalogError):
        ToolchainManager.from_catalog_path(catalog_path, home=tmp_path / "home", platform=_PLATFORM)


@pytest.mark.parametrize("malformed_root", [["a", "list"], "a scalar string", 42, None])
def test_from_catalog_raises_a_typed_error_for_a_non_mapping_root(malformed_root: object, tmp_path: Path) -> None:
    with pytest.raises(ToolchainCatalogError):
        ToolchainManager.from_catalog(malformed_root, home=tmp_path / "home", platform=_PLATFORM)


@pytest.mark.parametrize("malformed_distribution", [["a", "list"], "a scalar string", 42])
def test_from_catalog_raises_a_typed_error_for_a_non_mapping_distribution(
    malformed_distribution: object, tmp_path: Path
) -> None:
    catalog, _ = _catalog_and_digests()
    catalog["distribution"] = malformed_distribution

    with pytest.raises(ToolchainCatalogError):
        ToolchainManager.from_catalog(catalog, home=tmp_path / "home", platform=_PLATFORM)


@pytest.mark.parametrize("tool", sorted(_TOOLS))
def test_the_fake_archives_are_byte_reproducible(tool: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite digests one build of an archive and serves another.

    `_catalog_and_digests` pins a digest from one call and `_FakeDownloader`
    rebuilds the archive when the manager asks for it. Any container format
    stamping the current time makes those disagree across a second boundary,
    which surfaced as an intermittent `ToolchainVerificationError` on
    whichever tool happened to straddle it.

    The clock is moved between the two builds rather than trusting them to
    land in the same second, which is what made the original failure rare
    enough to read as unrelated flake.
    """
    specs = load_specs(_catalog_and_digests()[0], platform=_PLATFORM)
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    first = _archive_bytes(tool, specs[tool])
    monkeypatch.setattr(time, "time", lambda: 2_000_000.0)

    assert _archive_bytes(tool, specs[tool]) == first
