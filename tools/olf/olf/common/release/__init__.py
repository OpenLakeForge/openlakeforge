"""Release-bundle logic: component manifest, checksums, compatibility matrix,
and the release-readiness gate.

Split by capability into `_manifest` (manifest/checksums), `_compatibility`
(compatibility matrix rendering and drift checks), and `_readiness` (the
`olf release check` gate). This module re-exports only the surface consumed
by `olf.common.commands.release`.
"""

from __future__ import annotations

from olf.common.release._compatibility import render_compatibility_matrix
from olf.common.release._manifest import ReleaseError, build_manifest, load_catalog, render_manifest, write_checksums
from olf.common.release._readiness import run_release_check

__all__ = [
    "ReleaseError",
    "build_manifest",
    "load_catalog",
    "render_compatibility_matrix",
    "render_manifest",
    "run_release_check",
    "write_checksums",
]
