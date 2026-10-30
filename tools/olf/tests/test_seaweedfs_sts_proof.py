"""Self-check for docs/testing/seaweedfs_sts_proof.py (ADR 0019): the policies and configuration it gives SeaweedFS."""

from __future__ import annotations

import importlib.util
from pathlib import Path

PROOF = Path(__file__).resolve().parents[3] / "docs" / "testing" / "seaweedfs_sts_proof.py"
spec = importlib.util.spec_from_file_location("seaweedfs_sts_proof", PROOF)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def _actions(policy: dict) -> set[str]:
    return {a for s in policy["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}


def test_a_read_policy_grants_no_write_and_scopes_objects_to_the_table() -> None:
    policy = proof.polaris_session_policy(["sales_gold/orders"], [])

    assert not _actions(policy) & {"s3:PutObject", "s3:DeleteObject"}
    objects = next(s for s in policy["Statement"] if "s3:GetObject" in s["Action"])
    assert objects["Resource"] == ["arn:aws:s3:::gold/sales_gold/orders/*"]
    listing = next(s for s in policy["Statement"] if s["Action"] == "s3:ListBucket")
    assert listing["Condition"] == {"StringLike": {"s3:prefix": ["sales_gold/orders/*"]}}


def test_a_write_policy_adds_put_and_delete_on_the_write_location_only() -> None:
    policy = proof.polaris_session_policy(["sales_gold/orders"], ["sales_gold/orders"])

    writes = next(s for s in policy["Statement"] if "s3:PutObject" in s["Action"])
    assert writes["Resource"] == ["arn:aws:s3:::gold/sales_gold/orders/*"]


def test_only_the_polaris_identity_is_trusted_to_assume_the_vending_role() -> None:
    identities, iam, keys = proof.server_configs()

    (role,) = iam["roles"]
    principals = [s["Principal"]["AWS"] for s in role["trustPolicy"]["Statement"]]
    assert principals == [f"arn:aws:iam::{proof.ACCOUNT}:user/polaris"]
    assert {i["name"] for i in identities["identities"]} == set(keys)
