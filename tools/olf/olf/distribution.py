"""Versioned OpenLakeForge platform payloads embedded in the Python wheel.

The installed CLI is deliberately small Python packaging-wise: its immutable
Terraform/Helm/runtime tree travels as package data, then becomes a verified,
read-only payload below ``OLF_HOME``.  Source checkouts keep using their own
tree so contributor workflows remain unchanged.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from olf.project import ProjectSpec

_PAYLOAD_ROOTS = (
    "infra/terraform",
    "infra/helm/values",
    "infra/kind",
    "images",
    "packages/domain-model",
    "libs",
    "docs/schema",
    "lakehouse_code",
)
_PAYLOAD_FILES = ("openlakeforge.yaml", "release/component-catalog.yaml", "release/identity-roles.yaml")
_EXCLUDED_PARTS = frozenset({".terraform", ".tmp", "__pycache__", ".pytest_cache", ".venv", "dist", "build"})
_CATALOG_VERSION = re.compile(r"^distribution:\s*$.*?^\s+version:\s*['\"]?([^'\"\s#]+)", re.MULTILINE | re.DOTALL)
_ALPHA_VERSION = re.compile(r"^(\d+\.\d+\.\d+)-alpha\.(\d+)$")
_MANIFEST_NAME = "payload-manifest.json"


class DistributionError(RuntimeError):
    """A packaged platform payload cannot be trusted or located."""


def release_to_pep440(version: str) -> str:
    """Map the public alpha release syntax to the matching PEP 440 version."""
    match = _ALPHA_VERSION.fullmatch(version)
    if match is None:
        raise DistributionError(f"unsupported distribution version {version!r}; expected <x.y.z>-alpha.<n>")
    return f"{match.group(1)}a{match.group(2)}"


def _catalog_distribution_version(root: Path) -> str:
    catalog = root / "release" / "component-catalog.yaml"
    match = _CATALOG_VERSION.search(catalog.read_text(encoding="utf-8"))
    if match is None:
        raise DistributionError(f"could not read distribution.version from {catalog}")
    return match.group(1)


def distribution_version_at(root: Path) -> str | None:
    """The distribution version a resolved deployment root ships, or None when it ships no catalog.

    Activation gates a revision on the running distribution, and the root it
    must ask is the one the `DeploymentContext` already resolved -- not a
    second `runtime_layout()` read, which for an external project checkout
    resolves the project root rather than the distribution.
    """
    if not (root / "release" / "component-catalog.yaml").is_file():
        return None
    return _catalog_distribution_version(root)


def _tracked_payload_paths(root: Path) -> list[Path]:
    """Return the deliberately small, tracked runtime allowlist."""
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    paths: list[Path] = []
    for raw in result.stdout.decode("utf-8").split("\0"):
        if not raw:
            continue
        relative = PurePosixPath(raw)
        if any(part in _EXCLUDED_PARTS for part in relative.parts):
            continue
        if relative.name.startswith("sandbox.tfvars") or relative.name.startswith("terraform.tfstate"):
            continue
        value = relative.as_posix()
        included = value in _PAYLOAD_FILES or any(
            value == prefix or value.startswith(prefix + "/") for prefix in _PAYLOAD_ROOTS
        )
        if included:
            candidate = root / relative
            if candidate.is_file():
                paths.append(candidate)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _manifest_entry(root: Path, path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mode": path.stat().st_mode & 0o777,
    }


def build_embedded_payload(root: Path, *, archive: Path, metadata_path: Path) -> dict[str, object]:
    """Create the deterministic payload archive included in wheel and sdist builds."""
    root = root.resolve()
    version = _catalog_distribution_version(root)
    paths = _tracked_payload_paths(root)
    entries = [_manifest_entry(root, path) for path in paths]
    manifest = {"schema_version": 1, "distribution_version": version, "files": entries}
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as bundle:
                for path in paths:
                    relative = path.relative_to(root).as_posix()
                    info = tarfile.TarInfo(relative)
                    info.size = path.stat().st_size
                    info.mode = path.stat().st_mode & 0o777
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with path.open("rb") as source:
                        bundle.addfile(info, source)
                info = tarfile.TarInfo(_MANIFEST_NAME)
                info.size = len(manifest_data)
                info.mode = 0o444
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                bundle.addfile(info, io.BytesIO(manifest_data))

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    metadata: dict[str, object] = {
        "schema_version": 1,
        "distribution_version": version,
        "package_version": release_to_pep440(version),
        "archive": archive.name,
        "sha256": digest,
        "manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return metadata


def default_home(environ: dict[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = env.get("OLF_HOME")
    return Path(override).expanduser().resolve() if override else Path.home() / ".openlakeforge"


@dataclass(frozen=True)
class RuntimeLayout:
    mode: str
    distribution_root: Path
    project_root: Path
    state_root: Path
    work_root: Path
    cache_root: Path
    catalog_path: Path
    distribution_version: str
    payload_sha256: str | None

    @property
    def is_source(self) -> bool:
        return self.mode == "source"

    @property
    def project(self) -> ProjectSpec:
        """Resolve this layout's selected data project on demand."""
        from olf.project import ProjectSpec

        return ProjectSpec.from_layout(self)


@dataclass
class DistributionManager:
    home: Path
    metadata: dict[str, object]

    @classmethod
    def from_embedded(cls, *, home: Path | None = None) -> DistributionManager:
        try:
            data = resources.files("olf").joinpath("_embedded/platform.json").read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise DistributionError("this olf installation has no embedded platform payload") from exc
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise DistributionError("embedded platform metadata must be a JSON object")
        return cls(home=home or default_home(), metadata=parsed)

    @property
    def version(self) -> str:
        value = self.metadata.get("distribution_version")
        if not isinstance(value, str):
            raise DistributionError("embedded platform metadata has no distribution_version")
        return value

    @property
    def sha256(self) -> str:
        value = self.metadata.get("sha256")
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise DistributionError("embedded platform metadata has an invalid sha256")
        return value

    @property
    def payload_root(self) -> Path:
        return self.home / "distributions" / self.version / self.sha256 / "payload"

    @property
    def _lock_path(self) -> Path:
        return self.payload_root.parent / ".install.lock"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _archive_bytes(self) -> bytes:
        try:
            data = resources.files("olf").joinpath("_embedded/platform.tar.gz").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise DistributionError("embedded platform archive is missing") from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != self.sha256:
            raise DistributionError(f"embedded platform archive digest mismatch: expected {self.sha256}, got {actual}")
        return data

    def ensure(self) -> Path:
        """Install and validate the immutable payload, returning its root."""
        with self._locked():
            if self.payload_root.is_dir():
                self.verify()
                return self.payload_root

            staging = self.payload_root.parent / f".payload-{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            try:
                staging.mkdir(parents=True)
                archive_path = staging / "platform.tar.gz"
                archive_path.write_bytes(self._archive_bytes())
                extracted = staging / "payload"
                self._extract(archive_path, extracted)
                self._verify_root(extracted)
                self.payload_root.parent.mkdir(parents=True, exist_ok=True)
                os.replace(extracted, self.payload_root)
                for path in sorted(self.payload_root.rglob("*"), reverse=True):
                    if path.is_dir():
                        path.chmod(0o555)
                    else:
                        path.chmod(path.stat().st_mode & 0o111 | 0o444)
                self.payload_root.chmod(0o555)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return self.payload_root

    def verify(self) -> None:
        if not self.payload_root.is_dir():
            raise DistributionError(f"installed payload is missing: {self.payload_root}")
        self._verify_root(self.payload_root)

    def _extract(self, archive_path: Path, target: Path) -> None:
        target.mkdir(parents=True)
        with tarfile.open(archive_path, "r:gz") as bundle:
            names: set[str] = set()
            for member in bundle.getmembers():
                name = member.name
                relative = PurePosixPath(name)
                if (
                    not name
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or name in names
                    or not member.isfile()
                ):
                    raise DistributionError(f"unsafe embedded payload member: {name!r}")
                names.add(name)
                destination = (target / relative).resolve()
                if target.resolve() not in destination.parents:
                    raise DistributionError(f"payload member escapes extraction root: {name!r}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise DistributionError(f"payload member cannot be read: {name!r}")
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(member.mode & 0o777)
        if not (target / _MANIFEST_NAME).is_file():
            raise DistributionError("embedded payload has no payload manifest")

    def _verify_root(self, root: Path) -> None:
        try:
            manifest_data = (root / _MANIFEST_NAME).read_bytes()
            manifest = json.loads(manifest_data)
        except (OSError, json.JSONDecodeError) as exc:
            raise DistributionError("installed payload manifest is unreadable") from exc
        expected_manifest_digest = self.metadata.get("manifest_sha256")
        if (
            not isinstance(expected_manifest_digest, str)
            or hashlib.sha256(manifest_data).hexdigest() != expected_manifest_digest
        ):
            raise DistributionError("installed payload manifest digest mismatch")
        if not isinstance(manifest, dict) or manifest.get("distribution_version") != self.version:
            raise DistributionError("installed payload manifest has a mismatched distribution version")
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise DistributionError("installed payload manifest has no files list")
        expected: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise DistributionError("installed payload manifest has an invalid file entry")
            relative = entry.get("path")
            digest = entry.get("sha256")
            size = entry.get("size")
            if (
                not isinstance(relative, str)
                or not isinstance(digest, str)
                or not isinstance(size, int)
                or PurePosixPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts
            ):
                raise DistributionError("installed payload manifest has an unsafe file entry")
            expected.add(relative)
            candidate = root / PurePosixPath(relative)
            if not candidate.is_file() or candidate.stat().st_size != size:
                raise DistributionError(f"installed payload file is missing or changed: {relative}")
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual != digest:
                raise DistributionError(f"installed payload digest mismatch: {relative}")
        actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        if actual_paths != expected | {_MANIFEST_NAME}:
            raise DistributionError("installed payload contains undeclared or missing files")

    def clean(self) -> bool:
        target = self.payload_root.parent
        expected_target = (self.home / "distributions" / self.version / self.sha256).resolve()
        if target.resolve() != expected_target:
            raise DistributionError("refusing to clean a payload outside OLF_HOME")
        if not target.exists():
            return False
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o755)
        target.chmod(0o755)
        shutil.rmtree(target)
        return True


def runtime_layout(environ: dict[str, str] | None = None) -> RuntimeLayout:
    """Resolve source checkout or verified wheel payload paths."""
    env = environ if environ is not None else dict(os.environ)
    mode = env.get("OLF_DISTRIBUTION_MODE", "")
    if mode not in {"", "source", "installed"}:
        raise DistributionError("OLF_DISTRIBUTION_MODE must be 'source' or 'installed'")
    def checkout_root() -> Path | None:
        candidates = [Path.cwd()]
        try:
            candidates.append(Path(__file__).resolve().parents[3])
        except IndexError:
            pass
        for candidate in candidates:
            candidate = candidate.resolve()
            if (candidate / ".git").exists() and (candidate / "tools/olf/pyproject.toml").is_file():
                return candidate
        return None

    checkout = checkout_root()
    source_root = Path(
        env.get("OLF_DISTRIBUTION_ROOT") or env.get("OPENLAKEFORGE_REPO_ROOT") or checkout or "."
    ).resolve()
    embedded_available = False
    try:
        embedded_available = resources.files("olf").joinpath("_embedded/platform.json").is_file()
    except ModuleNotFoundError:
        pass
    source_override = "OLF_DISTRIBUTION_ROOT" in env or "OPENLAKEFORGE_REPO_ROOT" in env
    use_source = mode == "source" or (
        mode == "" and (source_override or checkout is not None or not embedded_available)
    )
    if use_source:
        catalog = source_root / "release" / "component-catalog.yaml"
        project = Path(env.get("OPENLAKEFORGE_PROJECT_ROOT", source_root)).resolve()
        return RuntimeLayout(
            mode="source",
            distribution_root=source_root,
            project_root=project,
            state_root=source_root,
            work_root=source_root / ".tmp",
            cache_root=source_root / ".tmp",
            catalog_path=catalog,
            distribution_version=_catalog_distribution_version(source_root) if catalog.is_file() else "unknown",
            payload_sha256=None,
        )

    manager = DistributionManager.from_embedded(home=default_home(env))
    root = manager.ensure()
    # A packaged distribution's payload is immutable and is never a user's
    # working directory.  Consumer commands therefore target the current
    # directory unless the caller explicitly selects another project.  This
    # is intentionally different from source mode, whose checkout remains
    # the contributor default.
    project = Path(env.get("OPENLAKEFORGE_PROJECT_ROOT", Path.cwd())).resolve()
    return RuntimeLayout(
        mode="installed",
        distribution_root=root,
        project_root=project,
        state_root=manager.home / "state",
        work_root=manager.home / "work",
        cache_root=manager.home / "cache",
        catalog_path=root / "release" / "component-catalog.yaml",
        distribution_version=manager.version,
        payload_sha256=manager.sha256,
    )
