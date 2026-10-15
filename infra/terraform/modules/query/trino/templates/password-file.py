"""Reads `user password` lines on stdin and writes Trino password.db lines.

PBKDF2-SHA1, because the bootstrap image has python3 but no openssl or
htpasswd. The format is the one docs/testing/trino_workload_auth_proof.py
showed Trino 480 accepting: user:iterations:salt:hash, salt and hash in hex.
"""

import hashlib
import secrets
import sys

ITERATIONS = 10000

for line in sys.stdin:
    user, password = line.split()
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha1", password.encode(), salt, ITERATIONS, dklen=32)
    print(f"{user}:{ITERATIONS}:{salt.hex()}:{digest.hex()}")
