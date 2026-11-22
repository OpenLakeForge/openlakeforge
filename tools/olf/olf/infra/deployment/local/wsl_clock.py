"""Detect two time sources fighting over a WSL2 clock (#937).

WSL2 sets its clock from Windows through Hyper-V time sync, by speeding the kernel
clock up or slowing it down. With systemd-timesyncd also running, NTP steps it back
every poll whenever Windows and NTP disagree. Every kind node shares that clock, so a
token Polaris issues just before a step looks issued in the future after it. Polaris
1.9.0 accepts up to 5 s of skew on a token's `iat` (apache/polaris#5766) and the steps
measured on WSL2 are about 1.4 s (#941), so this is reported as information, not a fault.
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
    """Why this host's clock steps backwards, with the fix should Polaris still refuse tokens; None when it does not."""
    if not is_wsl(osrelease) or not timesyncd_active():
        return None
    return (
        "systemd-timesyncd and WSL's Hyper-V time sync both set the clock, which then steps back "
        "every ~30 s. Polaris tolerates up to 5 s of that; if Trino still fails at random with "
        f"'Not authorized', run: {FIX}"
    )
