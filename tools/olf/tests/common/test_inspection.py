"""Unit tests for DoctorReport.as_dict() JSON serialization."""

from __future__ import annotations

from olf.common.deployment.inspection import DoctorReport, DoctorItem


def test_doctor_report_as_dict_all_passing() -> None:
    """DoctorReport.as_dict() emits valid JSON schema when all checks pass."""
    report = DoctorReport(
        items=[
            DoctorItem(name="host_tools", ok=True, detail="Docker, kubectl available"),
            DoctorItem(name="project_inputs", ok=True, detail="All required files present"),
            DoctorItem(name="cloud_auth", ok=True, detail="Authenticated to AWS"),
        ]
    )

    result = report.as_dict()

    assert result["command"] == "doctor"
    assert result["ok"] is True
    assert len(result["items"]) == 3
    assert result["items"][0] == {
        "id": "host_tools",
        "status": "pass",
        "message": "Docker, kubectl available",
        "remedy": None,
    }


def test_doctor_report_as_dict_with_failures() -> None:
    """DoctorReport.as_dict() marks failed items with status 'fail'."""
    report = DoctorReport(
        items=[
            DoctorItem(name="host_tools", ok=True, detail="Docker available"),
            DoctorItem(name="cloud_auth", ok=False, detail="AWS credentials not found"),
        ]
    )

    result = report.as_dict()

    assert result["command"] == "doctor"
    assert result["ok"] is False
    assert len(result["items"]) == 2
    assert result["items"][1] == {
        "id": "cloud_auth",
        "status": "fail",
        "message": "AWS credentials not found",
        "remedy": None,
    }


def test_doctor_report_as_dict_empty_items() -> None:
    """DoctorReport.as_dict() handles empty items list."""
    report = DoctorReport(items=[])

    result = report.as_dict()

    assert result["command"] == "doctor"
    assert result["ok"] is True
    assert result["items"] == []
