"""The Secrets bundle (`secrets.enc`): Kubernetes Secrets sealed under an operator passphrase.

scrypt turns the passphrase into an AES-256-GCM key. The header (magic, version, salt) is authenticated with the
ciphertext, so a changed header fails the same way a wrong passphrase does. Neither the passphrase nor a Secret
value is logged, printed or put in an error.
"""

from __future__ import annotations

import getpass
import json
import os
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from olf.infra.backup import BackupError

PASSPHRASE_VARIABLE = "OLF_BACKUP_PASSPHRASE"
_MAGIC = b"OLFSEC"
_VERSION = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12
_HEADER_BYTES = len(_MAGIC) + 1 + _SALT_BYTES
_BODY_BYTES = _HEADER_BYTES + _NONCE_BYTES
# Version 1 fixes these; changing one is a new version byte. n * r * 128 bytes = 32 MiB, a fraction of a second.
_SCRYPT_N = 2**15
_FIELDS = ("namespace", "name", "type", "data")


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=8, p=1).derive(passphrase.encode())


def encrypt_secrets(secrets: list[dict[str, Any]], passphrase: str) -> bytes:
    """Each secret is `{"namespace","name","type","data"}`, `data` base64 as Kubernetes stores it."""
    if not passphrase:
        raise BackupError("the backup passphrase is empty.")
    header = _MAGIC + bytes([_VERSION]) + os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    payload = json.dumps([{field: secret[field] for field in _FIELDS} for secret in secrets]).encode()
    return header + nonce + AESGCM(_key(passphrase, header[-_SALT_BYTES:])).encrypt(nonce, payload, header)


def decrypt_secrets(blob: bytes, passphrase: str) -> list[dict[str, Any]]:
    if len(blob) <= _BODY_BYTES or not blob.startswith(_MAGIC):
        raise BackupError("secrets.enc is not an OpenLakeForge Secrets bundle.")
    version = blob[len(_MAGIC)]
    if version != _VERSION:
        raise BackupError(f"secrets.enc is bundle version {version}; this olf reads version {_VERSION}.")
    header, nonce, sealed = blob[:_HEADER_BYTES], blob[_HEADER_BYTES:_BODY_BYTES], blob[_BODY_BYTES:]
    try:
        payload = AESGCM(_key(passphrase, header[-_SALT_BYTES:])).decrypt(nonce, sealed, header)
    except InvalidTag:
        raise BackupError("cannot decrypt secrets.enc: the passphrase is wrong, or the file was changed.") from None
    return json.loads(payload)


def read_passphrase(*, confirm: bool, environ: Mapping[str, str]) -> str:
    """`OLF_BACKUP_PASSPHRASE`, else a prompt (asked twice with `confirm`). Never argv."""
    if PASSPHRASE_VARIABLE in environ:
        passphrase = environ[PASSPHRASE_VARIABLE]
    else:
        passphrase = getpass.getpass("Backup passphrase: ")
        if confirm and getpass.getpass("Repeat the passphrase: ") != passphrase:
            raise BackupError("the two passphrases differ.")
    if not passphrase:
        raise BackupError("the backup passphrase is empty.")
    return passphrase
