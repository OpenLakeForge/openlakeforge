"""The Superset credentials Job (#400) creates the SECRET_KEY only when missing."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from test_postgresql_credentials_job import FAKE_KUBECTL

MODULE = Path(__file__).resolve().parents[3] / "infra/terraform/modules/analytics/superset"


def _script() -> str:
    # What Terraform renders into the Job: the two local names substituted into the heredoc.
    text = (MODULE / "credentials.tf").read_text()
    match = re.search(r"credentials_script = <<-SH\n(.*?)\n  SH\n", text, re.DOTALL)
    assert match
    return (
        match.group(1)
        .replace("${local.credentials_secret_name}", "superset-credentials")
        .replace("${local.legacy_env_secret_name}", "superset-env")
    )


def _run_job(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path / "secrets"), "NAMESPACE": "olf-dev"}
    return subprocess.run(["sh", "-ec", _script()], env=env, capture_output=True, text=True)


def test_a_fresh_install_gets_a_key(tmp_path: Path) -> None:
    result = _run_job(tmp_path)

    secret = tmp_path / "secrets/olf-dev/superset-credentials"
    assert result.returncode == 0, result.stderr
    assert len((secret / "secret-key").read_text()) == 64


def test_an_existing_chart_key_is_kept_so_stored_connection_passwords_stay_readable(tmp_path: Path) -> None:
    old = tmp_path / "secrets/olf-dev/superset-env"
    old.mkdir(parents=True)
    (old / "SUPERSET_SECRET_KEY").write_text("key-from-the-old-release")

    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "secrets/olf-dev/superset-credentials/secret-key").read_text() == "key-from-the-old-release"


def test_a_rerun_keeps_the_key_and_prints_no_value(tmp_path: Path) -> None:
    first = _run_job(tmp_path)
    secret = tmp_path / "secrets/olf-dev/superset-credentials"
    values = [(secret / "secret-key").read_text()]

    second = _run_job(tmp_path)

    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    assert [(secret / "secret-key").read_text()] == values
    assert not any(value in first.stdout + second.stdout for value in values)
