"""Detect two time sources fighting over a WSL2 clock (#937).

WSL2 sets its clock from Windows through Hyper-V time sync, by speeding the kernel
clock up or slowing it down. With systemd-timesyncd also running, NTP steps it back
every poll whenever Windows and NTP disagree. Every kind node shares that clock, so a
token Polaris issues just before a step looks issued in the future after it: Polaris
rejects it with an empty 401, and Trino queries fail with "Not authorized" or "Failed
to load table" at random.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from olf.common.deployment.errors import DeploymentError
from olf.common.tooling.process import ProcessRunner

FIX = "sudo systemctl disable --now systemd-timesyncd"
_OSRELEASE = Path("/proc/sys/kernel/osrelease")


def _timesyncd_active() -> bool:
    try:
        return ProcessRunner().run(["systemctl", "is-active", "--quiet", "systemd-timesyncd"], check=False).ok
    except DeploymentError:  # no systemctl
        return False


def is_wsl(osrelease: Path = _OSRELEASE) -> bool:
    try:
        return "microsoft" in osrelease.read_text().lower()
    except OSError:
        return False


def competing_time_sync(
    osrelease: Path = _OSRELEASE, timesyncd_active: Callable[[], bool] = _timesyncd_active
) -> str | None:
    """Why this host's clock steps backwards, with the fix; None when it does not."""
    if not is_wsl(osrelease) or not timesyncd_active():
        return None
    return (
        "systemd-timesyncd and WSL's Hyper-V time sync both set the clock, which then steps back "
        f"every ~30 s and makes Polaris reject Trino's tokens at random. Run: {FIX}"
    )
