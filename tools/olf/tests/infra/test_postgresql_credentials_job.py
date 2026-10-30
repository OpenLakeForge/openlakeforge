"""The PostgreSQL credentials Job (#400) creates each Secret only when missing, so Terraform holds no password."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[4] / "infra/terraform/modules/storage/postgresql/templates/credentials.sh.tftpl"
)

# A stand-in for kubectl that keeps Secrets as files, one directory per Secret.
FAKE_KUBECTL = """\
import base64, json, os, re, sys
from pathlib import Path

root, a = Path(os.environ["FAKE"]), sys.argv[1:]
ns = a[a.index("-n") + 1]
d = root / ns / a[3 if a[0] == "create" else 2]

if a[:2] == ["get", "secret"]:
    if not d.is_dir():
        sys.exit(0 if "--ignore-not-found" in a else 1)
    if "-o" in a:
        f = d / re.search(r'index \\.data "([^"]+)"', a[a.index("-o") + 1]).group(1)
        sys.stdout.write(base64.b64encode(f.read_bytes()).decode() if f.is_file() else "")
elif a[:3] == ["create", "secret", "generic"]:
    d.mkdir(parents=True)  # fails like the API server does when the Secret exists
    for x in (x for x in a if x.startswith("--from-literal=")):
        k, v = x[len("--from-literal="):].split("=", 1)
        (d / k).write_text(v)
elif a[:2] == ["patch", "secret"]:
    for k, v in json.loads(a[a.index("-p") + 1])["stringData"].items():
        (d / k).write_text(v)
else:
    sys.exit("unsupported: " + " ".join(a))
"""


def _run_job(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    # What templatefile renders: the variables and the `$${` escapes.
    script = (
        TEMPLATE.read_text()
        .replace("${admin_secret}", "postgresql-admin-creds")
        .replace("${polaris_secret}", "postgresql-polaris-creds")
        .replace("${polaris_user}", "polaris")
        .replace("${polaris_jdbc_url}", "jdbc:postgresql://pg:5432/polaris")
        .replace("${databases}", "dagster-creds=olf-system,olf-dev dex-creds=olf-system")
        .replace("$${", "${")
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path / "secrets"), "NAMESPACE": "olf-system"}
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True, text=True)


def test_every_secret_is_created_and_a_database_password_is_the_same_in_each_namespace(tmp_path: Path) -> None:
    result = _run_job(tmp_path)

    secrets = tmp_path / "secrets"
    assert result.returncode == 0, result.stderr
    assert len((secrets / "olf-system/postgresql-admin-creds/postgres-password").read_text()) == 32
    assert (secrets / "olf-system/postgresql-polaris-creds/jdbcUrl").read_text() == "jdbc:postgresql://pg:5432/polaris"
    assert (secrets / "olf-dev/dagster-creds/postgresql-password").read_text() == (
        secrets / "olf-system/dagster-creds/postgresql-password"
    ).read_text()
    assert (secrets / "olf-system/dex-creds/postgresql-password").is_file()


def test_a_rerun_rotates_nothing_and_prints_no_password(tmp_path: Path) -> None:
    first = _run_job(tmp_path)
    secrets = tmp_path / "secrets"
    values = {path: path.read_text() for path in secrets.rglob("*") if path.is_file()}

    second = _run_job(tmp_path)

    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    assert {path: path.read_text() for path in values} == values
    assert "Secret postgresql-admin-creds exists in olf-system; keeping it" in second.stdout
    assert not any(value in first.stdout + second.stdout for value in values.values() if len(value) == 32)


def test_a_namespace_copy_missing_after_the_first_run_gets_the_existing_password(tmp_path: Path) -> None:
    _run_job(tmp_path)
    copy = tmp_path / "secrets/olf-dev/dagster-creds"
    password = (copy / "postgresql-password").read_text()
    (copy / "postgresql-password").unlink()
    copy.rmdir()

    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (copy / "postgresql-password").read_text() == password
