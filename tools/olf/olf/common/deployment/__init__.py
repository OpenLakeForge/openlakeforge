"""Deployment state/context/orchestration primitives.

`olf.deployment` holds typed errors, retry policy, and `DeploymentContext` —
data/path/environment rules. It does not execute external processes; that is
`olf.tooling`'s job.
"""

from __future__ import annotations
