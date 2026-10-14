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
