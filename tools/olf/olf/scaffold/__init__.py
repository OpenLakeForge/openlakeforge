"""Golden-path scaffolding for `olf source|domain|product new`.

Every command builds a `ScaffoldPlan` (see `_shared.py`) describing the new
files and the edited `lakehouse_code/governance.yml`, then routes it through
`commit_plan` so file-safety and canonical-model validity are enforced once,
not per command.
"""

from __future__ import annotations
