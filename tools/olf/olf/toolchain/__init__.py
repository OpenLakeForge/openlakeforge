"""A versioned, private toolchain of external CLI binaries owned by `olf`.

`olf.toolchain` downloads, verifies, and activates the exact Terraform/Helm/
kubectl/kind versions pinned in `release/component-catalog.yaml`, scoped
under `OLF_HOME` (default `~/.openlakeforge`). It never touches a system
package manager and never mutates `PATH`. See `olf.tooling.resolver` for the
seam that substitutes this manager for `PathExecutableResolver`.
"""

from __future__ import annotations

from olf.toolchain.errors import ToolchainVerificationError
from olf.toolchain.manager import ToolchainManager
from olf.toolchain.platform import Platform, UnsupportedPlatformError
from olf.toolchain.spec import MANAGED_TOOLS, ToolSpec, load_specs

__all__ = [
    "MANAGED_TOOLS",
    "Platform",
    "ToolSpec",
    "ToolchainManager",
    "ToolchainVerificationError",
    "UnsupportedPlatformError",
    "load_specs",
]
