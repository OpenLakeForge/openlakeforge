"""Self-check for docs/testing/trino_workload_auth_proof.py (ADR 0016): the files it generates for Trino."""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

PROOF = Path(__file__).resolve().parents[3] / "docs" / "testing" / "trino_workload_auth_proof.py"
spec = importlib.util.spec_from_file_location("trino_workload_auth_proof", PROOF)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def test_password_line_is_a_pbkdf2_entry_that_verifies_the_password() -> None:
    user, iterations, salt, digest = proof.password_line("olf-dev-runtime", "s3cret").split(":")

    expected = hashlib.pbkdf2_hmac("sha1", b"s3cret", bytes.fromhex(salt), int(iterations), dklen=32)
    assert (user, digest) == ("olf-dev-runtime", expected.hex())


def test_each_principal_may_only_become_its_own_user_unless_extended() -> None:
    def may_become(rules: dict, who: str, user: str) -> bool:
        for rule in rules["principals"]:
            if re.fullmatch(rule["principal"], who) and re.fullmatch(rule["user"], user):
                return rule["allow"]
        return False

    base, extended = proof.access_rules(), proof.access_rules(acts_as=("dev", "alice"))
    dev, prod = proof.principal("dev"), proof.principal("prod")

    assert [may_become(base, dev, dev), may_become(base, dev, prod), may_become(base, "other", "other")] == [
        True,
        False,
        False,
    ]
    assert may_become(extended, dev, "alice") and not may_become(extended, prod, "alice")
    assert may_become(base, "openmetadata", "openmetadata") and not may_become(base, "openmetadata", prod)
