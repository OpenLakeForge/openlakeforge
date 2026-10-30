"""The password.db the Trino credential Job writes (ADR 0016) must verify the passwords it was given."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "infra/terraform/modules/query/trino/templates/password-file.py"


def test_each_line_is_a_pbkdf2_entry_that_verifies_its_password() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        input="olf-dev-runtime pw-one\nopenmetadata pw-two\n",
        capture_output=True,
        text=True,
        check=True,
    )

    def verifies(password: str, iterations: str, salt: str, digest: str) -> bool:
        derived = hashlib.pbkdf2_hmac("sha1", password.encode(), bytes.fromhex(salt), int(iterations), dklen=32)
        return derived.hex() == digest

    verified = {}
    for line in result.stdout.splitlines():
        user, *entry = line.split(":")
        verified[user] = {password: verifies(password, *entry) for password in ("pw-one", "pw-two")}

    assert verified == {
        "olf-dev-runtime": {"pw-one": True, "pw-two": False},
        "openmetadata": {"pw-one": False, "pw-two": True},
    }


# A stand-in for kubectl that keeps Secrets as files. Like the real one it reads a dotted key through
# JSONPath as nested fields (so nothing is found), and `index` in a go-template as the literal key.
FAKE_KUBECTL = '''\
import base64, json, os, re, sys
from pathlib import Path

root, a = Path(os.environ["FAKE"]), sys.argv[1:]
ns = a[a.index("-n") + 1] if "-n" in a else "default"
out = a[a.index("-o") + 1] if "-o" in a else ""


def sec(name, namespace=ns):
    return root / namespace / name


if a[:2] == ["get", "secret"]:
    d = sec(a[2])
    if not d.is_dir():
        sys.exit(0 if "--ignore-not-found" in a else 1)
    if out == "name":
        print("secret/" + a[2])
    elif out.startswith("jsonpath={.data."):
        f = d / out[len("jsonpath={.data."):-1]
        sys.stdout.write(base64.b64encode(f.read_bytes()).decode() if f.is_file() else "")
    elif out.startswith("go-template="):
        f = d / re.search(r'index \\.data "([^"]+)"', out).group(1)
        sys.stdout.write(base64.b64encode(f.read_bytes()).decode() if f.is_file() else "")
elif a[:3] == ["create", "secret", "generic"]:
    data = {}
    for x in a[4:]:
        if x.startswith("--from-literal="):
            k, v = x[15:].split("=", 1)
            data[k] = v.encode()
        elif x.startswith("--from-file="):
            k, path = x[12:].split("=", 1)
            data[k] = Path(path).read_bytes()
    if "--dry-run=client" in a:
        print(json.dumps({"name": a[3], "ns": ns, "data": {k: base64.b64encode(v).decode() for k, v in data.items()}}))
    else:
        sec(a[3]).mkdir(parents=True)
        for k, v in data.items():
            (sec(a[3]) / k).write_bytes(v)
elif a[0] == "replace":
    obj = json.loads(sys.stdin.read())
    for k, v in obj["data"].items():
        (sec(obj["name"], obj["ns"]) / k).write_bytes(base64.b64decode(v))
else:
    sys.exit("unsupported: " + " ".join(a))
'''
TEMPLATE = SCRIPT.with_name("credentials.sh.tftpl")


def _run_job(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    # What templatefile renders: the three variables and the `$${` escapes.
    script = (
        TEMPLATE.read_text()
        .replace("${principals}", "olf-dev-runtime openmetadata")
        .replace("${stage_namespaces}", "dev=olf-dev")
        .replace("${password_file_script}", SCRIPT.read_text())
        .replace("$${", "${")
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path / "secrets"), "NAMESPACE": "olf-system"}
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True, text=True)


def test_a_rerun_keeps_every_secret_including_the_one_with_a_dotted_key(tmp_path: Path) -> None:
    first = _run_job(tmp_path)
    internal = tmp_path / "secrets/olf-system/trino-internal-secret/internal-communication.shared-secret"
    password = tmp_path / "secrets/olf-system/trino-credential-olf-dev-runtime/password"
    created = (internal.read_text(), password.read_text())

    second = _run_job(tmp_path)

    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    assert "Created Secret trino-internal-secret" in first.stdout
    assert "Secret trino-internal-secret exists; keeping it" in second.stdout
    assert (internal.read_text(), password.read_text()) == created
    assert (tmp_path / "secrets/olf-dev/trino-credential/password").read_text() == created[1]


def test_a_principal_secret_without_its_password_is_refused_not_patched(tmp_path: Path) -> None:
    _run_job(tmp_path)
    (tmp_path / "secrets/olf-system/trino-credential-openmetadata/password").unlink()

    result = _run_job(tmp_path)

    assert result.returncode == 1
    assert "trino-credential-openmetadata exists without key password" in result.stderr
