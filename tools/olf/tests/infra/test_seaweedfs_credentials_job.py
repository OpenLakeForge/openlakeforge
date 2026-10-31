"""The SeaweedFS credentials Job (#400) creates each S3 key pair only when missing and builds the gateway config."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

from test_postgresql_credentials_job import FAKE_KUBECTL

MODULE = Path(__file__).resolve().parents[4] / "infra/terraform/modules/storage/seaweedfs"
TEMPLATE = MODULE / "templates/credentials.sh.tftpl"
KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
STAGE_ACTIONS = {
    stage: [
        *(
            f"{verb}:lakehouse-{stage}-{layer}"
            for layer in ("bronze", "silver", "gold")
            for verb in ("Read", "Write", "List")
        ),
        *(f"{verb}:ops/activations/{stage}/*" for verb in ("Read", "Write", "List")),
    ]
    for stage in ("dev", "prod")
}
IDENTITIES = [
    {"name": "openlakeforge-platform", "secret": "seaweedfs-s3-creds", "namespace": "olf-system", "actions": ["Admin"]},
    *(
        {
            "name": f"openlakeforge-{stage}",
            "secret": f"seaweedfs-{stage}-s3-creds",
            "namespace": f"olf-{stage}",
            "actions": a,
        }
        for stage, a in STAGE_ACTIONS.items()
    ),
]

# What credentials.tf renders as iam_config, reduced to the parts the Job touches.
IAM_CONFIG = {
    "sts": {"tokenDuration": "15m", "maxSessionLength": "1h", "issuer": "seaweedfs-sts"},
    "roles": [{"roleName": "polaris-vending", "roleArn": "arn:aws:iam::000000000000:role/polaris-vending"}],
}

# kubectl for Deployments, which the key Secrets' fake does not model: `get` finds one when DEPLOYED is set, and
# every `patch` is logged.
DEPLOYMENT_KUBECTL = """\
import os, sys
if sys.argv[2:3] == ["deployment"]:
    if sys.argv[1] == "patch":
        with open(os.environ["FAKE"] + ".patches", "a") as log:
            log.write(sys.argv[sys.argv.index("-p") + 1] + "\\n")
    elif os.environ.get("DEPLOYED"):
        print("deployment.apps/" + sys.argv[3])
    sys.exit(0)
"""


def _run_job(
    tmp_path: Path, identities: list[dict[str, object]] = IDENTITIES, *, deployed: bool = False
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{DEPLOYMENT_KUBECTL}{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    # What templatefile renders: the variables and the `$${` escapes.
    script = (
        TEMPLATE.read_text()
        .replace("${admin_secret}", "seaweedfs-s3-creds")
        .replace("${admin_namespaces}", "olf-system olf-extra")
        .replace("${namespace}", "olf-system")
        .replace("${auth_secret}", "seaweedfs-s3-auth")
        .replace("${s3_deployment}", "seaweedfs-s3")
        .replace("${identities}", json.dumps(identities, separators=(",", ":")))
        .replace("${scoped_pairs}", " ".join(f"{i['secret']}={i['namespace']}" for i in identities[1:]))
        .replace("${iam_config}", json.dumps(IAM_CONFIG, separators=(",", ":")))
        .replace("$${", "${")
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path / "secrets")} | (
        {"DEPLOYED": "1"} if deployed else {}
    )
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True, text=True)


def _values(secrets: Path) -> dict[Path, str]:
    return {path: path.read_text() for path in secrets.rglob("*") if path.is_file()}


def _config(tmp_path: Path) -> dict[str, list[dict[str, object]]]:
    return json.loads((tmp_path / "secrets/olf-system/seaweedfs-s3-auth/seaweedfs_s3_config").read_text())


def _iam(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "secrets/olf-system/seaweedfs-s3-auth/seaweedfs_iam_config").read_text())


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


def test_each_stage_key_is_granted_only_its_own_buckets(tmp_path: Path) -> None:
    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    identities = {identity["name"]: identity for identity in _config(tmp_path)["identities"]}
    assert identities["openlakeforge-platform"]["actions"] == ["Admin"]
    for stage, other in (("dev", "prod"), ("prod", "dev")):
        identity = identities[f"openlakeforge-{stage}"]
        key = tmp_path / f"secrets/olf-{stage}/seaweedfs-{stage}-s3-creds"
        assert identity["credentials"] == [
            {"accessKey": (key / KEYS[0]).read_text(), "secretKey": (key / KEYS[1]).read_text()}
        ]
        assert identity["actions"] == STAGE_ACTIONS[stage]
        assert not set(STAGE_ACTIONS[other]) & set(identity["actions"])


def test_a_rerun_writes_the_same_config_and_the_gateway_is_annotated_only_when_it_exists(tmp_path: Path) -> None:
    _run_job(tmp_path)
    config = (tmp_path / "secrets/olf-system/seaweedfs-s3-auth/seaweedfs_s3_config").read_text()
    assert not (tmp_path / "secrets.patches").exists()

    again = _run_job(tmp_path, deployed=True)

    assert again.returncode == 0, again.stderr
    assert (tmp_path / "secrets/olf-system/seaweedfs-s3-auth/seaweedfs_s3_config").read_text() == config
    assert config not in again.stdout
    assert "openlakeforge.io/s3-config-hash" in (tmp_path / "secrets.patches").read_text()


def test_a_new_stage_changes_the_config_and_so_the_annotation(tmp_path: Path) -> None:
    _run_job(tmp_path, deployed=True)
    first = (tmp_path / "secrets.patches").read_text()
    extra = {
        "name": "openlakeforge-qa",
        "secret": "seaweedfs-qa-s3-creds",
        "namespace": "olf-qa",
        "actions": ["List:q"],
    }

    result = _run_job(tmp_path, [*IDENTITIES, extra], deployed=True)

    assert result.returncode == 0, result.stderr
    assert [i["name"] for i in _config(tmp_path)["identities"]][-1] == "openlakeforge-qa"
    assert (tmp_path / "secrets.patches").read_text().splitlines()[-1] != first.splitlines()[-1]


def test_the_iam_file_carries_the_rendered_role_and_a_new_signing_key(tmp_path: Path) -> None:
    result = _run_job(tmp_path)

    assert result.returncode == 0, result.stderr
    iam = _iam(tmp_path)
    key = iam["sts"].pop("signingKey")
    assert iam == IAM_CONFIG
    assert len(base64.b64decode(key, validate=True)) == 32
    assert key not in result.stdout


def test_a_config_from_before_sts_gains_the_iam_file_and_keeps_its_signing_key_afterwards(tmp_path: Path) -> None:
    _run_job(tmp_path)
    (tmp_path / "secrets/olf-system/seaweedfs-s3-auth/seaweedfs_iam_config").unlink()

    first = _run_job(tmp_path, deployed=True)
    key = _iam(tmp_path)["sts"]["signingKey"]
    second = _run_job(tmp_path, deployed=True)

    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    assert _iam(tmp_path)["sts"]["signingKey"] == key
    patches = (tmp_path / "secrets.patches").read_text().splitlines()
    assert patches[-1] == patches[-2]


# The identity credentials.tf renders for the platform API with two stages' archive prefixes.
LOG_READER = {
    "name": "openlakeforge-platform-api-logs",
    "secret": "platform-api-log-reader-s3-creds",
    "namespace": "olf-system",
    "actions": [
        f"{verb}:ops/activations/{stage}/logs/k8s/*" for stage in ("dev", "prod") for verb in ("Read", "List")
    ],
}


def test_the_log_reader_gets_its_own_key_in_olf_system_and_only_read_and_list(tmp_path: Path) -> None:
    result = _run_job(tmp_path, [*IDENTITIES, LOG_READER])

    assert result.returncode == 0, result.stderr
    key = tmp_path / "secrets/olf-system/platform-api-log-reader-s3-creds"
    identity = next(i for i in _config(tmp_path)["identities"] if i["name"] == LOG_READER["name"])
    assert identity["credentials"] == [
        {"accessKey": (key / KEYS[0]).read_text(), "secretKey": (key / KEYS[1]).read_text()}
    ]
    assert identity["actions"] == LOG_READER["actions"]
    assert {action.split(":")[0] for action in identity["actions"]} == {"Read", "List"}
    others = {
        (tmp_path / "secrets" / str(i["namespace"]) / str(i["secret"]) / KEYS[0]).read_text() for i in IDENTITIES
    }
    assert (key / KEYS[0]).read_text() not in others
