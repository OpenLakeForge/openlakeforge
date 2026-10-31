"""The platform API's Superset users Job (ADR 0021, open item 3) mints each stage's password once, in the API's
namespace, and keeps the stage namespace's copy equal to it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_postgresql_credentials_job import FAKE_KUBECTL

TEMPLATE = (
    Path(__file__).resolve().parents[4]
    / "infra/terraform/modules/access/platform-api/templates/superset-users.sh.tftpl"
)
STAGES = {"platform-api-superset-dev": "olf-dev", "platform-api-superset-prod": "olf-prod"}


def _run_job(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    # What templatefile renders: the variables and the `$${` escapes.
    script = (
        TEMPLATE.read_text()
        .replace("${namespace}", "olf-system")
        .replace("${username}", "olf-platform-api")
        .replace("${stage_secret}", "platform-api-superset")
        .replace("${stage_secrets}", " ".join(f"{source}={ns}" for source, ns in STAGES.items()))
        .replace("$${", "${")
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path / "secrets")}
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True, text=True)


def _password(tmp_path: Path, namespace: str, name: str) -> str:
    return (tmp_path / "secrets" / namespace / name / "password").read_text()


def test_each_stage_gets_its_own_password_in_the_apis_namespace_and_a_copy_beside_superset(tmp_path: Path) -> None:
    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    dev, prod = (_password(tmp_path, "olf-system", source) for source in STAGES)
    assert len(dev) == 32 and dev.isalnum() and dev != prod
    assert _password(tmp_path, "olf-dev", "platform-api-superset") == dev
    assert _password(tmp_path, "olf-prod", "platform-api-superset") == prod
    assert (tmp_path / "secrets/olf-dev/platform-api-superset/username").read_text() == "olf-platform-api"


def test_a_rerun_keeps_every_password(tmp_path: Path) -> None:
    _run_job(tmp_path)
    before = {source: _password(tmp_path, "olf-system", source) for source in STAGES}

    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    assert {source: _password(tmp_path, "olf-system", source) for source in STAGES} == before
    assert "matches platform-api-superset-dev" in result.stdout


def test_a_stage_copy_that_drifted_is_overwritten_from_the_apis_namespace(tmp_path: Path) -> None:
    _run_job(tmp_path)
    (tmp_path / "secrets/olf-dev/platform-api-superset/password").write_text("tampered")

    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _password(tmp_path, "olf-dev", "platform-api-superset") == _password(
        tmp_path, "olf-system", "platform-api-superset-dev"
    )
    assert "Updated Secret platform-api-superset in olf-dev" in result.stdout
