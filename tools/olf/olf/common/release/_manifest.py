"""Release component manifest and checksums.

Shell/GitHub Actions remain the orchestrators for git, docker, cosign, and
syft invocations (ADR 0008); this module owns turning
`release/component-catalog.yaml` plus resolved image digests into the
component manifest and `checksums.txt` a consumer needs to verify a tagged
release.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CATALOG_PATH = "release/component-catalog.yaml"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+-alpha\.\d+$")
SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTION_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_SUFFIX_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")


class ReleaseError(ValueError):
    """Raised when release inputs are missing, inconsistent, or unpinned."""


@dataclass
class CheckResult:
    """Outcome of a single release-readiness gate check."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class ReleaseCheckReport:
    """Aggregate result of `olf release check`."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def render(self) -> str:
        lines = []
        for result in self.results:
            status = "PASS" if result.ok else "FAIL"
            detail = f": {result.detail}" if result.detail else ""
            lines.append(f"[{status}] {result.name}{detail}")
        return "\n".join(lines)


def load_catalog(catalog_path: str | Path = DEFAULT_CATALOG_PATH) -> dict[str, Any]:
    """Load and minimally validate the component catalog."""
    path = Path(catalog_path)
    if not path.is_file():
        raise ReleaseError(f"component catalog not found at {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ReleaseError(f"component catalog at {path} did not parse to a mapping")
    if data.get("kind") != "ComponentCatalog":
        raise ReleaseError(f"component catalog at {path} is missing kind: ComponentCatalog")
    distribution = data.get("distribution") or {}
    version = distribution.get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.match(version):
        raise ReleaseError(
            f"component catalog distribution.version {version!r} does not match "
            f"the expected alpha semver pattern (e.g. 0.1.0-alpha.1)"
        )
    return data


def catalog_version(catalog: dict[str, Any]) -> str:
    return str(catalog["distribution"]["version"])


def tag_for_version(version: str) -> str:
    return f"v{version}"


def version_for_tag(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def build_manifest(
    catalog: dict[str, Any],
    *,
    git_sha: str,
    image_digests: dict[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the resolved component manifest: catalog + resolved image digests + git SHA.

    `image_digests` maps image name (e.g. "project-code", "superset") to a
    fully qualified `repo@sha256:...` reference resolved at build time.
    """
    manifest: dict[str, Any] = {
        "apiVersion": "openlakeforge.io/v1alpha1",
        "kind": "ReleaseManifest",
        "distribution": {
            "version": catalog_version(catalog),
            "tag": tag_for_version(catalog_version(catalog)),
            "git_sha": git_sha,
        },
        "catalog": catalog,
        "resolved_images": dict(sorted((image_digests or {}).items())),
    }
    if generated_at:
        manifest["generated_at"] = generated_at
    return manifest


def render_manifest(manifest: dict[str, Any], fmt: str = "json") -> str:
    if fmt == "json":
        return json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    if fmt == "yaml":
        return yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
    raise ReleaseError(f"unsupported manifest format: {fmt!r} (expected 'json' or 'yaml')")


def compute_checksums(directory: str | Path, *, exclude: set[str] | None = None) -> list[tuple[str, str]]:
    """Compute sha256 checksums for every file in `directory` (non-recursive top level,
    recursive into subdirectories), returned as (relative_posix_path, hexdigest) sorted
    by path for determinism. `checksums.txt` itself is always excluded.
    """
    root = Path(directory)
    if not root.is_dir():
        raise ReleaseError(f"checksum directory not found: {root}")
    exclude = {"checksums.txt"} | (exclude or set())
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in exclude:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((rel, digest))
    return sorted(entries, key=lambda item: item[0])


def render_checksums(entries: list[tuple[str, str]]) -> str:
    # sha256sum -c compatible: "<digest>  <path>\n"
    return "".join(f"{digest}  {path}\n" for path, digest in entries)


def write_checksums(directory: str | Path, output: str | Path | None = None) -> Path:
    """Write a checksums manifest, excluding the manifest's own output path
    (even a custom one inside `directory`) so a rerun never hashes its own
    prior contents.
    """
    root = Path(directory)
    out_path = Path(output) if output else root / "checksums.txt"
    exclude: set[str] = set()
    try:
        exclude = {out_path.resolve().relative_to(root.resolve()).as_posix()}
    except ValueError:
        pass  # out_path is outside directory; nothing of its own to exclude.
    entries = compute_checksums(root, exclude=exclude)
    out_path.write_text(render_checksums(entries))
    return out_path
