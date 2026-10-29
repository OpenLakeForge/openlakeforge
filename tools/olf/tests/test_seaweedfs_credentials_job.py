"""The SeaweedFS credentials Job (#400) creates each S3 key pair only when missing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_postgresql_credentials_job import FAKE_KUBECTL

MODULE = Path(__file__).resolve().parents[3] / "infra/terraform/modules/storage/seaweedfs"
TEMPLATE = MODULE / "templates/credentials.sh.tftpl"
KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def _run_job(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    # What templatefile renders: the variables and the `$${` escapes.
    script = (
        TEMPLATE.read_text()
        .replace("${admin_secret}", "seaweedfs-s3-creds")
        .replace("${admin_namespaces}", "olf-system olf-extra")
        .replace("${stages}", "seaweedfs-dev-s3-creds=olf-dev seaweedfs-prod-s3-creds=olf-prod")
        .replace("$${", "${")
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path / "secrets")}
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True, text=True)


def _values(secrets: Path) -> dict[Path, str]:
    return {path: path.read_text() for path in secrets.rglob("*") if path.is_file()}


def test_a_fresh_install_creates_the_admin_pair_in_each_namespace_and_one_pair_per_stage(tmp_path: Path) -> None:
    result = _run_job(tmp_path)

    secrets = tmp_path / "secrets"
    assert result.returncode == 0, result.stderr
    for key in KEYS:
        assert (secrets / "olf-system/seaweedfs-s3-creds" / key).read_text() == (
            secrets / "olf-extra/seaweedfs-s3-creds" / key
        ).read_text()
    admin = secrets / "olf-system/seaweedfs-s3-creds"
    dev, prod = (secrets / f"olf-{stage}/seaweedfs-{stage}-s3-creds" for stage in ("dev", "prod"))
    assert (admin / "AWS_ACCESS_KEY_ID").read_text().startswith("olf")
    assert len((admin / "AWS_SECRET_ACCESS_KEY").read_text()) == 40
    assert len({(path / key).read_text() for path in (admin, dev, prod) for key in KEYS}) == 6


def test_a_rerun_keeps_every_key_and_prints_none(tmp_path: Path) -> None:
    first = _run_job(tmp_path)
    values = _values(tmp_path / "secrets")

    second = _run_job(tmp_path)

    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    assert _values(tmp_path / "secrets") == values
    assert not any(value in first.stdout + second.stdout for value in values.values())


def test_a_key_pair_an_earlier_release_made_is_adopted_and_copied(tmp_path: Path) -> None:
    old = tmp_path / "secrets/olf-system/seaweedfs-s3-creds"
    old.mkdir(parents=True)
    (old / "AWS_ACCESS_KEY_ID").write_text("olfoldaccesskey")
    (old / "AWS_SECRET_ACCESS_KEY").write_text("old-secret")

    result = _run_job(tmp_path)

    copy = tmp_path / "secrets/olf-extra/seaweedfs-s3-creds"
    assert result.returncode == 0, result.stderr
    assert (copy / "AWS_ACCESS_KEY_ID").read_text() == "olfoldaccesskey"
    assert (old / "AWS_SECRET_ACCESS_KEY").read_text() == "old-secret"
