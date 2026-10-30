from __future__ import annotations

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.commands import toolchain
from olf.toolchain.platform import UnsupportedPlatformError
from olf.toolchain.spec import ToolchainCatalogError

runner = CliRunner()


@pytest.mark.parametrize(
    "error",
    [
        UnsupportedPlatformError("Windows", "x86_64"),
        ToolchainCatalogError("release catalog not found"),
    ],
)
@pytest.mark.parametrize("command", [["toolchain", "list"], ["toolchain", "install"], ["toolchain", "path"]])
def test_manager_construction_failures_surface_as_a_clean_cli_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception, command: list[str]
) -> None:
    """`_manager()` failing with either error - an unsupported host or a
    missing/malformed catalog - must produce the same actionable exit
    rather than an uncaught traceback, since both are ToolchainError (#127)
    regardless of which internal module raised them."""

    def _raise() -> None:
        raise error

    monkeypatch.setattr(toolchain, "_manager", _raise)

    result = runner.invoke(app, command)

    assert result.exit_code != 0
    assert not isinstance(result.exception, (UnsupportedPlatformError, ToolchainCatalogError))


def test_clean_surfaces_manager_construction_failures_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """`clean` must be able to report a construction failure cleanly even
    though its own job is removing old cached toolchains - it shouldn't
    need a working platform/catalog to at least report the error."""
    def _raise() -> None:
        raise UnsupportedPlatformError("Windows", "x86_64")

    monkeypatch.setattr(toolchain, "_manager", _raise)

    result = runner.invoke(app, ["toolchain", "clean", "--all"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, UnsupportedPlatformError)


def test_platform_and_catalog_errors_are_toolchain_errors() -> None:
    """The hierarchy fix itself: both error types the CLI must catch belong
    to the same family `olf.commands.toolchain` actually catches."""
    from olf.toolchain.errors import ToolchainError

    assert isinstance(UnsupportedPlatformError("Windows", "x86_64"), ToolchainError)
    assert isinstance(ToolchainCatalogError("bad catalog"), ToolchainError)


@pytest.mark.parametrize("command", [["toolchain", "install"], ["toolchain", "path", "terraform"]])
@pytest.mark.parametrize(
    "native_error",
    [PermissionError("permission denied: /some/path"), OSError("no space left on device")],
)
def test_provisioning_failures_normalize_to_a_clean_cli_error(
    monkeypatch: pytest.MonkeyPatch, command: list[str], native_error: Exception
) -> None:
    """`olf toolchain install`/`path` call `ToolchainManager` directly,
    bypassing `ManagedExecutableResolver`'s catch-and-wrap - a native I/O or
    archive-extraction failure the manager doesn't wrap itself (a
    permission error, a full disk, a corrupt archive) must still surface as
    the CLI's normal concise failure, not a raw traceback."""

    class _FailingManager:
        specs = {"terraform": object()}

        def resolve(self, tool: str):  # noqa: ANN202
            raise native_error

        def ensure_all(self):  # noqa: ANN202
            raise native_error

    monkeypatch.setattr(toolchain, "_manager", lambda: _FailingManager())

    result = runner.invoke(app, command)

    assert result.exit_code != 0
    assert not isinstance(result.exception, type(native_error))
    assert str(native_error) in result.output


def test_clean_normalizes_a_prune_failure_to_a_clean_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingManager:
        def prune(self, **kwargs):  # noqa: ANN003, ANN202
            raise OSError("no space left on device")

    monkeypatch.setattr(toolchain, "_manager", lambda: _FailingManager())

    result = runner.invoke(app, ["toolchain", "clean", "--all"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, OSError)
    assert "no space left on device" in result.output
