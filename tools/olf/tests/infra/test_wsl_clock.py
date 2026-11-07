from __future__ import annotations

from pathlib import Path

from olf.infra.deployment.local import wsl_clock


def _osrelease(tmp_path: Path, release: str) -> Path:
    path = tmp_path / "osrelease"
    path.write_text(release + "\n")
    return path


def test_wsl_with_timesyncd_reports_the_fix(tmp_path: Path) -> None:
    release = _osrelease(tmp_path, "6.18.54.1-microsoft-standard-WSL2")
    problem = wsl_clock.competing_time_sync(release, timesyncd_active=lambda: True)
    assert problem is not None and wsl_clock.FIX in problem


def test_wsl_without_timesyncd_is_fine(tmp_path: Path) -> None:
    release = _osrelease(tmp_path, "6.18.54.1-microsoft-standard-WSL2")
    assert wsl_clock.competing_time_sync(release, timesyncd_active=lambda: False) is None


def test_other_linux_hosts_are_not_checked(tmp_path: Path) -> None:
    release = _osrelease(tmp_path, "6.8.0-45-generic")
    assert wsl_clock.competing_time_sync(release, timesyncd_active=lambda: True) is None


def test_hosts_without_osrelease_are_not_wsl(tmp_path: Path) -> None:
    assert not wsl_clock.is_wsl(tmp_path / "missing")
