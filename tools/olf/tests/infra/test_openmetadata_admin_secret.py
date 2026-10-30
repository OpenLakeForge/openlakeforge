"""The OpenMetadata bootstrap Job (#400) creates the admin password Secret only when missing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_postgresql_credentials_job import FAKE_KUBECTL

TEMPLATE = (
    Path(__file__).resolve().parents[4]
    / "infra/terraform/modules/governance/openmetadata/templates/bootstrap.sh.tftpl"
)


def _run_prefix(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    # The part of the script that runs before OpenMetadata is contacted.
    prefix = TEMPLATE.read_text().split("# Wait for OpenMetadata")[0]
    script = (
        prefix.replace("${om_url}", "http://om").replace("${admin_email}", "a@b").replace(
            "${admin_secret_name}", "openmetadata-admin"
        )
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path / "secrets"), "NAMESPACE": "olf-system"}
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True, text=True)


def test_the_password_meets_openmetadatas_policy(tmp_path: Path) -> None:
    result = _run_prefix(tmp_path)

    password = (tmp_path / "secrets/olf-system/openmetadata-admin/admin-password").read_text()
    assert result.returncode == 0, result.stderr
    assert 8 <= len(password) <= 56
    assert any(c.isupper() for c in password) and any(c.islower() for c in password)
    assert any(c.isdigit() for c in password) and any(not c.isalnum() for c in password)


def test_a_rerun_keeps_the_password_and_prints_nothing(tmp_path: Path) -> None:
    first = _run_prefix(tmp_path)
    path = tmp_path / "secrets/olf-system/openmetadata-admin/admin-password"
    password = path.read_text()

    second = _run_prefix(tmp_path)

    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    assert path.read_text() == password
    assert password not in first.stdout + second.stdout + first.stderr + second.stderr
