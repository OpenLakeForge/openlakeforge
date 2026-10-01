"""OpenLakeForge deployment tooling."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("openlakeforge")
except PackageNotFoundError:
    # Editable/source execution before package metadata exists. The release
    # command validates this fallback against release/component-catalog.yaml.
    __version__ = "0.3.0a1"
