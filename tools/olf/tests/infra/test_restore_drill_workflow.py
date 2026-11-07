"""The restore drill (#913) is a composite action that the nightly full e2e runs; nothing runs it before GitHub does.

These tests read the checked-in YAML for the properties that must not regress: the order of the steps, the
passphrase never appearing unmasked, and the backup directory never leaving the runner.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[4]
ACTION = yaml.safe_load((ROOT / ".github/actions/restore-drill/action.yml").read_text())
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/local-full-e2e.yml").read_text())
DRILL_STEPS: list[dict[str, Any]] = ACTION["runs"]["steps"]
JOB_STEPS: list[dict[str, Any]] = WORKFLOW["jobs"]["e2e"]["steps"]
DRILL_DIRECTORY = PurePosixPath(ACTION["inputs"]["directory"]["default"])
UPLOAD = "actions/upload-artifact@"


def _first_step_running(command: str) -> int:
    return next(index for index, step in enumerate(DRILL_STEPS) if command in step["run"])


def test_the_drill_captures_backs_up_destroys_rebuilds_restores_verifies_then_scans() -> None:
    order = [
        "olf infra e2e durability capture",
        "olf infra backup",
        "olf infra destroy",
        "olf infra platform apply",
        "olf infra restore",
        "olf infra e2e durability verify",
        "olf infra e2e secret-scan",
    ]

    positions = [_first_step_running(command) for command in order]

    assert positions == sorted(set(positions))


def test_the_nightly_runs_the_drill_after_every_step_that_needs_the_cluster() -> None:
    drill = next(index for index, step in enumerate(JOB_STEPS) if step.get("uses") == "./.github/actions/restore-drill")

    later = [step.get("uses", "") + step.get("run", "") for step in JOB_STEPS[drill + 1 :]]

    assert all("deploy-stage" not in text and "olf infra e2e run" not in text for text in later)
    assert "schedule" in JOB_STEPS[drill]["if"]


def test_the_passphrase_is_masked_before_use_and_only_ever_written_to_the_job_environment() -> None:
    prepare = DRILL_STEPS[0]["run"]
    uses = [line.strip() for line in prepare.splitlines() if "${passphrase}" in line]

    assert uses[0].startswith('echo "::add-mask::')
    assert uses[1:] == ['echo "OLF_BACKUP_PASSPHRASE=${passphrase}"']
    assert '>> "${GITHUB_ENV}"' in prepare
    assert not any("--passphrase" in step["run"] for step in DRILL_STEPS)


def test_the_scan_covers_the_backup_and_logs_and_skips_only_the_encrypted_bundle() -> None:
    scan = DRILL_STEPS[_first_step_running("secret-scan")]["run"]

    assert '--path "${DRILL_DIR}/backup"' in scan and '--path "${DRILL_DIR}/logs"' in scan
    assert "--exclude secrets.enc" in scan and scan.count("--exclude") == 1
    assert "--also-env OLF_BACKUP_PASSPHRASE" in scan


def _uploaded_paths() -> list[PurePosixPath]:
    return [
        PurePosixPath(line.strip())
        for step in JOB_STEPS
        if step.get("uses", "").startswith(UPLOAD)
        for line in str(step["with"]["path"]).splitlines()
        if line.strip()
    ]


def test_no_upload_can_carry_the_backup_directory() -> None:
    backup = DRILL_DIRECTORY / "backup"

    for path in _uploaded_paths():
        assert path != backup and backup not in path.parents and path not in backup.parents, path


def test_the_drill_publishes_only_its_report_and_the_report_holds_only_the_manifest_and_verify_logs() -> None:
    report = DRILL_DIRECTORY / "report"
    upload = next(step for step in JOB_STEPS if step.get("name") == "Upload the restore drill report")
    written = [
        token
        for step in DRILL_STEPS
        for token in step["run"].replace('"', " ").split()
        if "${DRILL_DIR}/report/" in token
    ]

    assert PurePosixPath(upload["with"]["path"]) == report
    assert written
    assert all(token.endswith("/report/manifest.json") or "/report/verify-" in token for token in written)


def test_access_tls_none_switches_only_the_conformance_profiles_access_tls() -> None:
    """#958: a dispatch can run the drill on the plain-HTTP local default; the nightly keeps local-ca."""
    access_tls = WORKFLOW[True]["workflow_dispatch"]["inputs"]["access_tls"]
    init = next(step for step in JOB_STEPS if "openlakeforge.conformance.yaml" in step.get("run", ""))["run"]
    profile_text = (ROOT / "openlakeforge.conformance.yaml").read_text()
    original = yaml.safe_load(profile_text)

    switched = yaml.safe_load(
        "\n".join("    tls: none" if line == "    tls: local-ca" else line for line in profile_text.splitlines())
    )

    assert access_tls["default"] == "local-ca"
    assert "sed -i 's/^    tls: local-ca$/    tls: none/' openlakeforge.yaml" in init
    assert original["spec"]["access"]["tls"] == "local-ca"
    assert switched["spec"]["access"]["tls"] == "none"
    assert switched["spec"]["identity"] == original["spec"]["identity"]
    assert {**switched["spec"], "access": original["spec"]["access"]} == original["spec"]
