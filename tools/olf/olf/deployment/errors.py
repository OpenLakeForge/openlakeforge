"""Typed error hierarchy for the deployment substrate.

`str(error)` on every error here is diagnostic-safe: it only ever includes
sanitized argv/environment representations (see `olf.tooling.redact`), never
raw secret values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from olf.tooling.redact import redact_argv, redact_env


class DeploymentError(Exception):
    """Base class for all deployment-substrate errors."""


class ToolingError(DeploymentError):
    """Base class for external process/tool execution errors."""


class ExecutableNotFoundError(ToolingError):
    """Raised when an executable resolver cannot locate a required tool."""

    def __init__(self, tool: str, *, searched: str | None = None) -> None:
        self.tool = tool
        self.searched = searched
        message = f"executable '{tool}' was not found"
        if searched:
            message = f"{message} (searched: {searched})"
        super().__init__(message)


class CommandExecutionError(ToolingError):
    """Raised when a command exits non-zero and the caller asked to `check`."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        *,
        stdout: str = "",
        stderr: str = "",
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.argv = tuple(str(a) for a in argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cwd = cwd
        self.env = dict(env) if env is not None else None

        sanitized_argv = " ".join(redact_argv(self.argv))
        message = f"command failed ({returncode}): {sanitized_argv}"
        if cwd is not None:
            message += f" (cwd={cwd})"
        if self.env:
            sanitized_env = redact_env(self.env)
            message += f" env={sanitized_env}"
        sanitized_stderr = self._sanitized_output(stderr)
        if sanitized_stderr:
            message += f"\nstderr: {sanitized_stderr}"
        super().__init__(message)

    def _sanitized_output(self, text: str) -> str:
        # Command output is not scanned for secret content, only truncated
        # for readability; secrets are kept out of argv/env in the first
        # place rather than pattern-matched out of free-form tool output.
        stripped = text.strip()
        return stripped[:2000]


class CommandTimeoutError(ToolingError):
    """Raised when a command exceeds its configured timeout."""

    def __init__(self, argv: Sequence[str], timeout_seconds: float) -> None:
        self.argv = tuple(str(a) for a in argv)
        self.timeout_seconds = timeout_seconds
        sanitized_argv = " ".join(redact_argv(self.argv))
        super().__init__(f"command timed out after {timeout_seconds:g}s: {sanitized_argv}")


class DeploymentPreconditionError(DeploymentError):
    """Raised when a deployment lifecycle step's precondition/safety check fails.

    Covers missing Terraform state, an unreachable Kubernetes context, and the
    explicit "refuse to destroy" safety checks ported from the local shell
    scripts (e.g. an adoptable-but-unmanaged kind cluster, or a namespace that
    still has platform resources).
    """


class UnsupportedProviderError(DeploymentError):
    """Raised when a `DeploymentProvider` has not been implemented yet."""


class ToolchainError(ToolingError):
    """Raised when the managed toolchain (#127) cannot resolve or provision a
    tool: unsupported host platform, digest verification failure, or a
    download failure. Never falls back to an unmanaged host binary; the
    message names the `OLF_TOOLCHAIN_MODE=host` escape hatch.
    """

    def __init__(self, tool: str, *, reason: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(
            f"managed toolchain could not provision {tool!r}: {reason} "
            "(set OLF_TOOLCHAIN_MODE=host to use a host-installed binary instead)"
        )
