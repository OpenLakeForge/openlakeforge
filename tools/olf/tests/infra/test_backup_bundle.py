"""The encrypted Secrets bundle (#910)."""

from __future__ import annotations

import base64

import pytest

from olf.infra.backup import BackupError
from olf.infra.backup._bundle import decrypt_secrets, encrypt_secrets, read_passphrase

_VALUE = base64.b64encode(b"hunter2-hunter2-hunter2").decode()
_SECRETS = [
    {"namespace": "olf-system", "name": "polaris-bootstrap-credentials", "type": "Opaque", "data": {"k": _VALUE}}
]


def test_a_bundle_round_trips_and_holds_no_plaintext() -> None:
    blob = encrypt_secrets(_SECRETS, "correct horse")

    assert decrypt_secrets(blob, "correct horse") == _SECRETS
    assert _VALUE.encode() not in blob and b"polaris-bootstrap-credentials" not in blob


def test_a_wrong_passphrase_fails_clearly() -> None:
    blob = encrypt_secrets(_SECRETS, "correct horse")

    with pytest.raises(BackupError, match="passphrase is wrong") as error:
        decrypt_secrets(blob, "battery staple")

    assert "correct horse" not in str(error.value)


def test_a_changed_header_or_body_fails_like_a_wrong_passphrase() -> None:
    blob = bytearray(encrypt_secrets(_SECRETS, "correct horse"))
    for index in (7, len(blob) - 1):  # the salt, the tag
        changed = bytearray(blob)
        changed[index] ^= 1
        with pytest.raises(BackupError, match="cannot decrypt"):
            decrypt_secrets(bytes(changed), "correct horse")


def test_something_else_is_not_a_bundle() -> None:
    with pytest.raises(BackupError, match="not an OpenLakeForge Secrets bundle"):
        decrypt_secrets(b"not a bundle at all, just text, long enough", "x")


def test_the_passphrase_comes_from_the_environment_or_a_confirmed_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    assert read_passphrase(confirm=True, environ={"OLF_BACKUP_PASSPHRASE": "from-env"}) == "from-env"

    answers = iter(["typed", "typed"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert read_passphrase(confirm=True, environ={}) == "typed"

    answers = iter(["typed", "other"])
    with pytest.raises(BackupError, match="differ"):
        read_passphrase(confirm=True, environ={})


def test_an_empty_passphrase_is_refused() -> None:
    with pytest.raises(BackupError, match="empty"):
        read_passphrase(confirm=False, environ={"OLF_BACKUP_PASSPHRASE": ""})
    with pytest.raises(BackupError, match="empty"):
        encrypt_secrets(_SECRETS, "")
