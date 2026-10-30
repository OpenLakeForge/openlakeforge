#!/usr/bin/env python3
"""Reproduction of the SeaweedFS STS session-policy proof (ADR 0019, section 3, verification item 1).

Polaris vends storage credentials by calling STS AssumeRole with a session policy scoped to one table's location.
This starts the pinned SeaweedFS binary (no cluster, no Docker), configures one role the way Polaris would assume it,
requests credentials with the session policy Polaris 1.4.0 builds, and prints PASS/FAIL per case: a read credential
must reach its own table and nothing else, a write credential must write only its own table.

    uv run --project tools/olf python docs/testing/seaweedfs_sts_proof.py [--weed PATH]

Without --weed it downloads the image pinned in release/component-catalog.yaml from Docker Hub by digest, checks
every manifest and layer against its sha256, and runs the image's /usr/bin/weed. Everything lives in a temporary
directory removed on exit.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[2]
ACCOUNT = "000000000000"  # SeaweedFS's defaultAccountID: identities are arn:aws:iam::000000000000:user/<name>
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/polaris-vending"
BUCKET = "gold"
OWN_TABLE = "sales_gold/orders"
OTHER_TABLE = "hr_gold/salaries"
# A sibling whose path starts with the table's path: a policy matching on a bare prefix would leak it.
SIBLING_TABLE = "sales_gold/orders_archive"
REGISTRY = "https://registry-1.docker.io/v2"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def pinned_image(name: str = "seaweedfs") -> tuple[str, str]:
    match = re.search(
        rf"^\s+{name}: (\S+):\S+@(sha256:\S+)$",
        (REPO / "release/component-catalog.yaml").read_text(),
        re.M,
    )
    if not match:
        raise SystemExit(f"no pinned {name} image in release/component-catalog.yaml")
    return match.group(1), match.group(2)


def _verified(url: str, digest: str, token: str, accept: str | None = None) -> bytes:
    headers = {
        "Authorization": f"Bearer {token}",
        **({"Accept": accept} if accept else {}),
    }
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=600
    ) as response:
        body = response.read()
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise SystemExit(f"{url}: content does not match {digest}")
    return body


def fetch_weed(work: Path) -> Path:
    """The pinned image's /usr/bin/weed, every blob checked against the digest that names it."""
    repository, digest = pinned_image()
    query = urllib.parse.urlencode(
        {"service": "registry.docker.io", "scope": f"repository:{repository}:pull"}
    )
    with urllib.request.urlopen(
        f"https://auth.docker.io/token?{query}", timeout=60
    ) as response:
        token = json.load(response)["token"]
    manifest = json.loads(
        _verified(
            f"{REGISTRY}/{repository}/manifests/{digest}", digest, token, MANIFEST_TYPES
        )
    )
    if "manifests" in manifest:
        platform = next(
            m
            for m in manifest["manifests"]
            if m["platform"] == {"architecture": "amd64", "os": "linux"}
        )
        url = f"{REGISTRY}/{repository}/manifests/{platform['digest']}"
        manifest = json.loads(_verified(url, platform["digest"], token, MANIFEST_TYPES))
    rootfs = work / "rootfs"
    for layer in manifest["layers"]:
        blob = _verified(
            f"{REGISTRY}/{repository}/blobs/{layer['digest']}", layer["digest"], token
        )
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            archive.extractall(rootfs, filter="tar")
    print(
        f"verified {repository}@{digest} (linux/amd64, {len(manifest['layers'])} layers)"
    )
    return rootfs / "usr/bin/weed"


def polaris_session_policy(
    read: list[str], write: list[str], *, allow_list: bool = True
) -> dict:
    """The session policy Polaris 1.4.0 builds for a table (AwsCredentialsStorageIntegration.policyString)."""
    statements: list[dict] = []
    if write:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{BUCKET}/{path}/*" for path in write],
            }
        )
    locations = list(dict.fromkeys(read + write))
    if allow_list:
        statements.append(
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{BUCKET}",
                "Condition": {
                    "StringLike": {"s3:prefix": [f"{path}/*" for path in locations]}
                },
            }
        )
    statements.append(
        {
            "Effect": "Allow",
            "Action": "s3:GetBucketLocation",
            "Resource": f"arn:aws:s3:::{BUCKET}",
        }
    )
    statements.append(
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": [f"arn:aws:s3:::{BUCKET}/{path}/*" for path in locations],
        }
    )
    return {"Version": "2012-10-17", "Statement": statements}


def server_configs() -> tuple[dict, dict, dict[str, tuple[str, str]]]:
    """SeaweedFS's identity file, its advanced IAM file, and each identity's keys."""
    keys = {
        name: (f"{name.upper()}KEY", secrets.token_hex(20))
        for name in ("admin", "polaris", "reader")
    }
    actions = {"admin": ["Admin"], "polaris": ["Admin"], "reader": ["Read"]}
    identities = {
        "identities": [
            {
                "name": name,
                "credentials": [
                    {"accessKey": keys[name][0], "secretKey": keys[name][1]}
                ],
                "actions": actions[name],
            }
            for name in keys
        ]
    }
    iam = {
        "sts": {
            "tokenDuration": "1h",
            "maxSessionLength": "12h",
            "issuer": "seaweedfs-sts",
            "signingKey": base64.b64encode(secrets.token_bytes(32)).decode(),
        },
        "policy": {"defaultEffect": "Deny", "storeType": "memory"},
        "policies": [
            {
                "name": "gold-full",
                "document": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:*"],
                            "Resource": [
                                f"arn:aws:s3:::{BUCKET}",
                                f"arn:aws:s3:::{BUCKET}/*",
                            ],
                        }
                    ],
                },
            }
        ],
        "roles": [
            {
                "roleName": "polaris-vending",
                "roleArn": ROLE_ARN,
                "trustPolicy": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "AWS": f"arn:aws:iam::{ACCOUNT}:user/polaris"
                            },
                            "Action": ["sts:AssumeRole"],
                        }
                    ],
                },
                "attachedPolicies": ["gold-full"],
            }
        ],
    }
    return identities, iam, keys


def _bindable(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _free_port(taken: set[int]) -> int:
    """A port whose gRPC twin is free too: SeaweedFS listens for gRPC on port + 10000."""
    while True:
        port = secrets.randbelow(20_000) + 20_000
        pair = {port, port + 10_000}
        if not pair & taken and all(_bindable(p) for p in pair):
            taken |= pair
            return port


def start_server(
    weed: Path, work: Path, identities: dict, iam: dict
) -> tuple[subprocess.Popen, str]:
    (work / "data").mkdir()
    (work / "s3.json").write_text(json.dumps(identities))
    (work / "iam.json").write_text(json.dumps(iam))
    taken: set[int] = set()
    s3_port = _free_port(taken)
    ports = {name: _free_port(taken) for name in ("master", "volume", "filer")}
    argv = [
        str(weed), "server", "-ip=127.0.0.1", "-ip.bind=127.0.0.1", f"-dir={work / 'data'}",
        f"-master.port={ports['master']}", f"-volume.port={ports['volume']}", f"-filer.port={ports['filer']}",
        "-s3", f"-s3.port={s3_port}", f"-s3.config={work / 's3.json'}", f"-s3.iam.config={work / 'iam.json'}",
    ]  # fmt: skip
    log = (work / "weed.log").open("w")
    process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = "\n".join((work / "weed.log").read_text().splitlines()[-20:])
            raise SystemExit(f"weed exited with {process.returncode}:\n{tail}")
        try:
            with socket.create_connection(("127.0.0.1", s3_port), timeout=1):
                return process, f"http://127.0.0.1:{s3_port}"
        except OSError:
            time.sleep(0.5)
    process.kill()
    raise SystemExit("the S3 port did not open within 90 seconds")


def client(
    service: str, endpoint: str, access: str, secret: str, token: str | None = None
):
    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        aws_session_token=token,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
        ),
    )


# Each case names the exact result it expects, so an unrelated failure (NoSuchKey,
# InternalError, NotImplemented) cannot pass as a denial.
ALLOWED = "allowed"
DENIED = "AccessDenied"
BAD_TOKEN = "InvalidAccessKeyId"


def _outcome(call: Callable[[], object]) -> str:
    try:
        call()
    except ClientError as exc:
        return exc.response["Error"]["Code"]
    return ALLOWED


def run(weed: Path, work: Path) -> int:
    identities, iam, keys = server_configs()
    process, endpoint = start_server(weed, work, identities, iam)
    try:
        # The S3 API may answer before the IAM integration has loaded its roles.
        time.sleep(3)
        admin = client("s3", endpoint, *keys["admin"])
        admin.create_bucket(Bucket=BUCKET)
        for table in (OWN_TABLE, OTHER_TABLE, SIBLING_TABLE):
            admin.put_object(
                Bucket=BUCKET, Key=f"{table}/data/00000.parquet", Body=b"PAR1"
            )

        def credentials(caller: str, policy: dict | None) -> dict:
            sts = client("sts", endpoint, *keys[caller])
            extra = {"Policy": json.dumps(policy)} if policy else {}
            response = sts.assume_role(
                RoleArn=ROLE_ARN,
                RoleSessionName="PolarisAwsCredentialsStorageIntegration",
                DurationSeconds=900,
                **extra,
            )
            return response["Credentials"]

        def s3(creds: dict, *, token: str | None = None):
            return client(
                "s3",
                endpoint,
                creds["AccessKeyId"],
                creds["SecretAccessKey"],
                token or creds["SessionToken"],
            )

        read_policy = polaris_session_policy([OWN_TABLE], [])
        write_policy = polaris_session_policy([OWN_TABLE], [OWN_TABLE])
        print(
            f"read session policy: {len(json.dumps(read_policy))} characters (SeaweedFS limit 2048)"
        )
        read = s3(credentials("polaris", read_policy))
        write = s3(credentials("polaris", write_policy))
        full = s3(credentials("polaris", None))
        reader = client("s3", endpoint, *keys["reader"])
        read_creds = credentials("polaris", read_policy)
        token = read_creds["SessionToken"]
        tampered = s3(
            read_creds,
            token=token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB"),
        )

        def get(s3c, table: str) -> Callable[[], object]:
            return lambda: s3c.get_object(
                Bucket=BUCKET, Key=f"{table}/data/00000.parquet"
            )["Body"].read()

        def put(s3c, table: str) -> Callable[[], object]:
            return lambda: s3c.put_object(
                Bucket=BUCKET, Key=f"{table}/data/00001.parquet", Body=b"PAR1"
            )

        def delete(s3c, table: str) -> Callable[[], object]:
            return lambda: s3c.delete_object(
                Bucket=BUCKET, Key=f"{table}/data/00001.parquet"
            )

        def listing(s3c, prefix: str | None) -> Callable[[], object]:
            extra = {"Prefix": prefix} if prefix is not None else {}
            return lambda: s3c.list_objects_v2(Bucket=BUCKET, **extra)

        def assume(caller: str) -> Callable[[], object]:
            sts = client("sts", endpoint, *keys[caller])
            return lambda: sts.assume_role(
                RoleArn=ROLE_ARN, RoleSessionName="probe", DurationSeconds=900
            )

        cases: list[tuple[str, str, Callable[[], object], str]] = [
            ("r1", "read credential, GET own table", get(read, OWN_TABLE), ALLOWED),
            (
                "r2",
                "read credential, GET another namespace's table",
                get(read, OTHER_TABLE),
                DENIED,
            ),
            (
                "r3",
                f"read credential, GET sibling {SIBLING_TABLE}",
                get(read, SIBLING_TABLE),
                DENIED,
            ),
            ("r4", "read credential, PUT own table", put(read, OWN_TABLE), DENIED),
            (
                "r5",
                "read credential, LIST own table prefix",
                listing(read, f"{OWN_TABLE}/"),
                ALLOWED,
            ),
            (
                "r6",
                "read credential, LIST another table's prefix",
                listing(read, f"{OTHER_TABLE}/"),
                DENIED,
            ),
            (
                "r7",
                "read credential, LIST the whole bucket",
                listing(read, None),
                DENIED,
            ),
            ("w1", "write credential, PUT own table", put(write, OWN_TABLE), ALLOWED),
            (
                "w2",
                "write credential, DELETE own table",
                delete(write, OWN_TABLE),
                ALLOWED,
            ),
            (
                "w3",
                "write credential, PUT another table",
                put(write, OTHER_TABLE),
                DENIED,
            ),
            (
                "w4",
                "write credential, GET another table",
                get(write, OTHER_TABLE),
                DENIED,
            ),
            (
                "c1",
                "control: no session policy, GET another table",
                get(full, OTHER_TABLE),
                ALLOWED,
            ),
            (
                "c2",
                "read credential with a tampered session token",
                get(tampered, OWN_TABLE),
                BAD_TOKEN,
            ),
            (
                "a1",
                "AssumeRole by an identity neither trusted nor granted sts:AssumeRole",
                assume("reader"),
                DENIED,
            ),
            # The workloads' static keys (Floe, Trino) must keep their legacy authorization with STS enabled.
            (
                "s1",
                "static Read identity, GET any table",
                get(reader, OTHER_TABLE),
                ALLOWED,
            ),
            ("s2", "static Read identity, PUT", put(reader, OWN_TABLE), DENIED),
        ]
        failures = 0
        for case, description, call, expected in cases:
            outcome = _outcome(call)
            ok = outcome == expected
            failures += not ok
            print(
                f"{'PASS' if ok else 'FAIL'}  {case:3} {description}: expected {expected}, got {outcome}"
            )
        return 1 if failures else 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--weed",
        type=Path,
        help="a SeaweedFS binary to run instead of the pinned image's",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="olf-seaweedfs-sts-proof-") as tmp:
        work = Path(tmp)
        weed = args.weed.resolve() if args.weed else fetch_weed(work)
        print(
            subprocess.run(
                [str(weed), "version"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        return run(weed, work)


if __name__ == "__main__":
    sys.exit(main())
