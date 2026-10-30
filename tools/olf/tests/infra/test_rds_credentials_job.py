"""The RDS bootstrap Job (#400) creates each database password only when missing and never stores the master one."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_postgresql_credentials_job import FAKE_KUBECTL

MODULE = Path(__file__).resolve().parents[4] / "infra/terraform/modules/storage/rds-postgresql"
TEMPLATE = MODULE / "templates/credentials.sh.tftpl"

# Stands in for Secrets Manager: the RDS-managed secret is a JSON document.
FAKE_AWS = """#!/bin/sh
printf '%s' '{"username":"postgres","password":"rds-managed-secret"}'
"""
FAILING_AWS = """#!/bin/sh
echo AccessDeniedException >&2
exit 254
"""


def _run_job(tmp_path: Path, aws: str = FAKE_AWS) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "kubectl").write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    (bin_dir / "aws").write_text(aws)
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    master = tmp_path / "master"
    master.mkdir(exist_ok=True)
    # What templatefile renders: the variable and the `$${` escapes.
    script = (
        TEMPLATE.read_text()
        .replace("${databases}", "dagster-creds=olf-shared,olf-dev superset-creds=olf-shared")
        .replace("$${", "${")
        .replace("/master/", f"{master}/")
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE": str(tmp_path / "secrets"),
        "NAMESPACE": "olf-shared",
        "AWS_REGION": "eu-west-1",
        "MASTER_SECRET_ARN": "arn:aws:secretsmanager:eu-west-1:1:secret:rds",
    }
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True, text=True)


def test_a_password_is_shared_by_every_namespace_and_the_master_one_is_only_handed_over(tmp_path: Path) -> None:
    result = _run_job(tmp_path)

    secrets = tmp_path / "secrets"
    assert result.returncode == 0, result.stderr
    shared = (secrets / "olf-shared/dagster-creds/postgresql-password").read_text()
    assert len(shared) == 32
    assert (secrets / "olf-dev/dagster-creds/postgresql-password").read_text() == shared
    assert (secrets / "olf-shared/superset-creds/postgresql-password").is_file()
    assert (tmp_path / "master/password").read_text() == "rds-managed-secret"
    assert not any("rds-managed-secret" in path.read_text() for path in secrets.rglob("*") if path.is_file())


def test_a_rerun_and_an_existing_secret_keep_their_value_and_print_none(tmp_path: Path) -> None:
    existing = tmp_path / "secrets/olf-shared/dagster-creds"
    existing.mkdir(parents=True)
    (existing / "postgresql-password").write_text("password-from-v0.2")

    first = _run_job(tmp_path)
    second = _run_job(tmp_path)

    secrets = tmp_path / "secrets"
    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    assert (secrets / "olf-shared/dagster-creds/postgresql-password").read_text() == "password-from-v0.2"
    assert (secrets / "olf-dev/dagster-creds/postgresql-password").read_text() == "password-from-v0.2"
    assert "password-from-v0.2" not in first.stdout + second.stdout + first.stderr + second.stderr


def test_a_failed_master_password_fetch_fails_the_job_without_writing_anything(tmp_path: Path) -> None:
    result = _run_job(tmp_path, aws=FAILING_AWS)

    assert result.returncode != 0
    assert not (tmp_path / "master/password").exists()


def test_a_password_that_only_a_consumer_namespace_holds_is_kept_everywhere(tmp_path: Path) -> None:
    # v0.2 left the Dagster password in the stage namespace alone.
    existing = tmp_path / "secrets/olf-dev/dagster-creds"
    existing.mkdir(parents=True)
    (existing / "postgresql-password").write_text("password-from-v0.2")

    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "secrets/olf-shared/dagster-creds/postgresql-password").read_text() == "password-from-v0.2"
