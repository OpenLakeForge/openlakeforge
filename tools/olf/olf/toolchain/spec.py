"""Per-tool download specs: URL templates, archive layout, and catalog loading.

Upstream URL layout is typed Python, not catalog data — only the pinned
version and per-platform digest come from `release/component-catalog.yaml`
(`components.toolchain`). That keeps the catalog's shape symmetric with its
existing `components.images`/`components.actions` blocks: a version plus a
digest, nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard

from olf.toolchain.errors import ToolchainError
from olf.toolchain.platform import Platform

ArchiveKind = Literal["zip", "tar.gz", "raw"]

MANAGED_TOOLS: tuple[str, ...] = ("terraform", "helm", "kubectl", "kind")

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ToolchainCatalogError(ToolchainError):
    """Raised when `components.toolchain` in the catalog is missing or malformed."""


@dataclass(frozen=True)
class ToolSpec:
    """Everything needed to fetch, verify, and activate one managed tool."""

    name: str
    version: str
    platform: Platform
    sha256: str
    url: str
    archive: ArchiveKind
    member: str | None = None
    """Path of the executable inside the archive; unused when `archive == "raw"`."""


def _terraform_url(version: str, platform: Platform) -> str:
    return (
        f"https://releases.hashicorp.com/terraform/{version}/"
        f"terraform_{version}_{platform.os}_{platform.arch}.zip"
    )


def _helm_url(version: str, platform: Platform) -> str:
    return f"https://get.helm.sh/helm-v{version}-{platform.os}-{platform.arch}.tar.gz"


def _kubectl_url(version: str, platform: Platform) -> str:
    return f"https://dl.k8s.io/release/v{version}/bin/{platform.os}/{platform.arch}/kubectl"


def _kind_url(version: str, platform: Platform) -> str:
    return (
        f"https://github.com/kubernetes-sigs/kind/releases/download/v{version}/"
        f"kind-{platform.os}-{platform.arch}"
    )


_BUILDERS: dict[str, tuple[Any, ArchiveKind, str | None]] = {
    "terraform": (_terraform_url, "zip", "terraform"),
    "helm": (_helm_url, "tar.gz", "{os}-{arch}/helm"),
    "kubectl": (_kubectl_url, "raw", None),
    "kind": (_kind_url, "raw", None),
}


def is_valid_digest(value: object) -> TypeGuard[str]:
    """Whether `value` is a well-formed `sha256:<64 hex chars>` digest
    string - shared between catalog validation here and receipt validation
    in `manager.py`, so both reject the same malformed shapes."""
    return isinstance(value, str) and bool(_SHA256_PATTERN.match(value))


def _digest(value: object, *, tool: str, platform_key: str) -> str:
    if not is_valid_digest(value):
        raise ToolchainCatalogError(
            f"components.toolchain.{tool}.platforms.{platform_key} must be 'sha256:<64 hex chars>', got {value!r}"
        )
    return value


def build_spec(tool: str, entry: Mapping[str, Any], *, platform: Platform) -> ToolSpec:
    if tool not in _BUILDERS:
        raise ToolchainCatalogError(f"unmanaged tool {tool!r}; expected one of {MANAGED_TOOLS}")
    version = entry.get("version")
    if not isinstance(version, str) or not version:
        raise ToolchainCatalogError(f"components.toolchain.{tool}.version must be a non-empty string")
    platforms = entry.get("platforms")
    if not isinstance(platforms, Mapping):
        raise ToolchainCatalogError(f"components.toolchain.{tool}.platforms must be a mapping")
    if platform.key not in platforms:
        raise ToolchainCatalogError(f"components.toolchain.{tool}.platforms is missing {platform.key!r}")
    sha256 = _digest(platforms[platform.key], tool=tool, platform_key=platform.key)

    url_builder, archive, member_template = _BUILDERS[tool]
    member = member_template.format(os=platform.os, arch=platform.arch) if member_template else None
    return ToolSpec(
        name=tool,
        version=version,
        platform=platform,
        sha256=sha256,
        url=url_builder(version, platform),
        archive=archive,
        member=member,
    )


def activated_filename(name: str, sha256: str) -> str:
    """The content-addressed filename an activated executable is stored
    under: `<tool>-<digest>`, not just `<tool>`.

    Two different pins of the same tool (e.g. two checkouts sharing
    `OLF_HOME` at the same `distribution.version` but different catalog
    pins) must never share one mutable path - a caller that resolved one
    pin could otherwise have its already-returned path silently swapped
    out by another process installing a different pin before the caller's
    subprocess actually runs it. Naming by digest makes every activated
    file immutable once written: the same digest always means the same
    bytes, so reusing an existing file written by another process is
    always safe.
    """
    return f"{name}-{sha256.removeprefix('sha256:')}"


def load_specs(catalog: Mapping[str, Any], *, platform: Platform) -> dict[str, ToolSpec]:
    """Build every managed `ToolSpec` declared in `catalog` for `platform`."""
    components = catalog.get("components")
    if components is not None and not isinstance(components, Mapping):
        raise ToolchainCatalogError(f"release catalog's components must be a mapping, got {type(components).__name__}")
    toolchain = (components or {}).get("toolchain")
    if not isinstance(toolchain, Mapping):
        raise ToolchainCatalogError("release catalog is missing components.toolchain")
    missing = [tool for tool in MANAGED_TOOLS if tool not in toolchain]
    if missing:
        raise ToolchainCatalogError(f"components.toolchain is missing entries for {missing}")
    return {tool: build_spec(tool, toolchain[tool], platform=platform) for tool in MANAGED_TOOLS}
