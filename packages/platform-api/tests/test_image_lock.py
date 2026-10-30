"""The image installs exactly what the package's own lock resolves."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_the_image_lock_pins_the_runtime_set_of_uv_lock() -> None:
    lock = tomllib.loads((ROOT / "packages/platform-api/uv.lock").read_text())
    image = (ROOT / "images/platform-api/requirements.lock").read_text()
    pinned = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", image, re.MULTILINE))
    versions = {package["name"]: package.get("version") for package in lock["package"]}

    assert pinned, "requirements.lock pins nothing"
    stale = {name: (version, versions.get(name)) for name, version in pinned.items() if versions.get(name) != version}
    assert not stale, f"images/platform-api/requirements.lock differs from uv.lock; re-export it (README): {stale}"
