"""Plan report for terraform change detection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanReport:
    """Result of a Terraform plan operation."""

    changes_pending: bool
    phase: str

    @property
    def ok(self) -> bool:
        return True  # plan succeeded (not about whether changes exist)

    def render(self) -> str:
        return "Terraform changes are pending." if self.changes_pending else "Terraform reports no changes."

    def as_dict(self) -> dict[str, object]:
        """Return JSON-serializable dict for --json output."""
        return {
            "command": "plan",
            "ok": True,  # plan succeeded - CI should branch on changes_pending, not ok
            "phase": self.phase,
            "changes_pending": self.changes_pending,  # bool | None - None on error
            "items": [
                {
                    "id": f"{self.phase}_plan",
                    "status": "pending" if self.changes_pending else "clean",
                    "message": self.render(),
                    "remedy": "Run 'olf infra deploy' to apply changes" if self.changes_pending else None,
                }
            ],
        }
