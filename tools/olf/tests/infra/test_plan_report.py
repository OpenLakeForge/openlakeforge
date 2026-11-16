"""Unit tests for PlanReport.as_dict() JSON serialization."""

from __future__ import annotations

from olf.infra.deployment.plan import PlanReport


def test_plan_report_as_dict_changes_pending() -> None:
    """PlanReport.as_dict() reports changes_pending=true and status='pending' when changes exist."""
    report = PlanReport(changes_pending=True, phase="platform")

    result = report.as_dict()

    assert result["command"] == "plan"
    assert result["ok"] is True  # plan succeeded
    assert result["phase"] == "platform"
    assert result["changes_pending"] is True
    assert len(result["items"]) == 1
    assert result["items"][0] == {
        "id": "platform_plan",
        "status": "pending",
        "message": "Terraform changes are pending.",
        "remedy": "Run 'olf infra deploy' to apply changes",
    }


def test_plan_report_as_dict_no_changes() -> None:
    """PlanReport.as_dict() reports changes_pending=false and status='clean' when no changes."""
    report = PlanReport(changes_pending=False, phase="foundation")

    result = report.as_dict()

    assert result["command"] == "plan"
    assert result["ok"] is True
    assert result["phase"] == "foundation"
    assert result["changes_pending"] is False
    assert len(result["items"]) == 1
    assert result["items"][0] == {
        "id": "foundation_plan",
        "status": "clean",
        "message": "Terraform reports no changes.",
        "remedy": None,
    }


def test_plan_report_ok_property() -> None:
    """PlanReport.ok is always True (plan succeeded, regardless of changes)."""
    pending = PlanReport(changes_pending=True, phase="all")
    clean = PlanReport(changes_pending=False, phase="all")

    assert pending.ok is True
    assert clean.ok is True
