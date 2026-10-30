from __future__ import annotations

import pytest

from olf.toolchain.platform import Platform, UnsupportedPlatformError


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-amd64"),
        ("Linux", "x86_64", "linux-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("linux", "amd64", "linux-amd64"),
    ],
)
def test_from_uname_maps_known_platforms(system: str, machine: str, expected: str) -> None:
    platform = Platform.from_uname(system=system, machine=machine)
    assert platform.key == expected
    assert str(platform) == expected


def test_from_uname_rejects_unknown_os() -> None:
    with pytest.raises(UnsupportedPlatformError):
        Platform.from_uname(system="Windows", machine="x86_64")


def test_from_uname_rejects_unknown_arch() -> None:
    with pytest.raises(UnsupportedPlatformError):
        Platform.from_uname(system="Linux", machine="mips")


def test_detect_returns_a_platform() -> None:
    assert isinstance(Platform.detect(), Platform)
