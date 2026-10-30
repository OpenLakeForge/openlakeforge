"""Deployment state/context/orchestration primitives.

`olf.common.deployment` holds typed errors, retry policy, and `DeploymentContext` —
data/path/environment rules. It does not execute external processes; that is
`olf.common.tooling`'s job.
"""

from __future__ import annotations
