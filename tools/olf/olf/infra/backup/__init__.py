"""`olf infra backup`: a restorable copy of the data a deployment holds (#910), and what `restore` reads (#911)."""

from __future__ import annotations


class BackupError(RuntimeError):
    """A backup cannot be taken, or a backup directory cannot be trusted."""
