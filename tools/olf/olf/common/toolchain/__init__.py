"""A versioned, private toolchain of external CLI binaries owned by `olf`.

`olf.common.toolchain` downloads, verifies, and activates the exact Terraform/Helm/
kubectl/kind versions pinned in `release/component-catalog.yaml`, scoped
under `OLF_HOME` (default `~/.openlakeforge`). It never touches a system
package manager and never mutates `PATH`. See `olf.common.tooling.resolver` for the
seam that substitutes this manager for `PathExecutableResolver`.
"""

from __future__ import annotations

from olf.common.toolchain.errors import ToolchainVerificationError
from olf.common.toolchain.manager import ToolchainManager
from olf.common.toolchain.platform import Platform, UnsupportedPlatformError
from olf.common.toolchain.spec import MANAGED_TOOLS, ToolSpec, load_specs

__all__ = [
    "MANAGED_TOOLS",
    "Platform",
    "ToolSpec",
    "ToolchainManager",
    "ToolchainVerificationError",
    "UnsupportedPlatformError",
    "load_specs",
]
