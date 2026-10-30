"""End-to-end validation for OpenLakeForge environments.

Split by capability into `_shell` (config/error type + process/kubectl
primitives), `_runner` (environment setup, suite dispatch, full-suite
assertion inventory), `_health`, `_dagster`, `_trino`, `_layers`,
`_assertions` (OpenMetadata/Superset), `_artifacts`, `_ingress` (Traefik
restart and certificate renewal drills), and `_preflight`
(AWS provider checks). This module re-exports only the surface consumed by
`olf.commands.e2e`.
"""

from __future__ import annotations

from olf.e2e._runner import run
from olf.e2e._shell import E2EError

__all__ = ["E2EError", "run"]
