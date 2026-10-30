"""Host platform detection for the managed toolchain.

Only the four platforms OpenLakeForge's release pipeline builds kind/CI
images for are supported: darwin/linux x amd64/arm64.
"""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass

from olf.toolchain.errors import ToolchainError


class UnsupportedPlatformError(ToolchainError):
    """Raised when the host OS/architecture has no managed toolchain build."""

    def __init__(self, system: str, machine: str) -> None:
        self.system = system
        self.machine = machine
        super().__init__(
            f"no managed toolchain is published for {system}/{machine}; "
            "set OLF_TOOLCHAIN_MODE=host and install Terraform/Helm/kubectl/kind yourself"
        )


_OS_NAMES = {"darwin": "darwin", "linux": "linux"}
_ARCH_NAMES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


@dataclass(frozen=True)
class Platform:
    """A managed-toolchain platform identity, e.g. `darwin-arm64`."""

    os: str
    arch: str

    @property
    def key(self) -> str:
        return f"{self.os}-{self.arch}"

    def __str__(self) -> str:
        return self.key

    @classmethod
    def detect(cls) -> Platform:
        return cls.from_uname(system=_platform.system(), machine=_platform.machine())

    @classmethod
    def from_uname(cls, *, system: str, machine: str) -> Platform:
        os_name = _OS_NAMES.get(system.lower())
        arch_name = _ARCH_NAMES.get(machine.lower())
        if os_name is None or arch_name is None:
            raise UnsupportedPlatformError(system, machine)
        return cls(os=os_name, arch=arch_name)
