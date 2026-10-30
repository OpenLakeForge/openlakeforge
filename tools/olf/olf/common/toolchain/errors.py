"""Errors raised while resolving or installing a managed tool.

These are internal to `olf.toolchain`; `olf.tooling.resolver` catches them
and re-raises `olf.deployment.errors.ToolchainError` (a `DeploymentError`),
matching how every other deployment failure surfaces to `olf doctor` and the
CLI's `--phase`/lifecycle commands.
"""

from __future__ import annotations


class ToolchainError(RuntimeError):
    """Base class for `olf.toolchain` failures."""


class ToolchainDownloadError(ToolchainError):
    """Raised when a tool archive could not be downloaded."""


class ToolchainVerificationError(ToolchainError):
    """Raised when a downloaded artifact's digest does not match the catalog."""

    def __init__(self, tool: str, *, expected: str, actual: str) -> None:
        self.tool = tool
        self.expected = expected
        self.actual = actual
        super().__init__(f"digest mismatch for {tool!r}: expected {expected}, got {actual}")
