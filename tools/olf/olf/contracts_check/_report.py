"""Result types shared by every `olf contracts check` check."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CheckResult:
    """Outcome of a single contract check."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class ContractsCheckReport:
    """Aggregate result of `olf contracts check`."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def render(self) -> str:
        lines = []
        for result in self.results:
            status = "PASS" if result.ok else "FAIL"
            detail = f": {result.detail}" if result.detail else ""
            lines.append(f"[{status}] {result.name}{detail}")
        return "\n".join(lines)
