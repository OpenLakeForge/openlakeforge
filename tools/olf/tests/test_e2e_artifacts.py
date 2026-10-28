import dataclasses
from pathlib import Path
from typing import Any

import pytest
from conftest import E2E_INVENTORY

from olf.e2e import _artifacts
from olf.e2e._shell import E2EConfig, E2EError


def test_check_ops_artifacts_uses_configured_bucket_for_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bucket_waits: list[tuple[str, str]] = []
    artifact_checks: list[tuple[str, str, str]] = []

    class FakeS3Client:
        pass

    local_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=E2E_INVENTORY,
        seaweedfs_local_port=19000,
    )

    monkeypatch.setattr(_artifacts, "trigger_log_archive_job", lambda _cfg: None)
    monkeypatch.setattr(
        _artifacts,
        "load_provider_contracts_or_raise",
        lambda _cfg: {"shared": {"ops_storage": {"bucket_name": "custom-ops-bucket"}}},
    )
    monkeypatch.setattr(_artifacts.k8s, "secret_value", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(
        _artifacts.k8s,
        "port_forward",
        lambda *_args, **_kwargs: __import__("contextlib").nullcontext(),
    )
    monkeypatch.setattr(_artifacts.boto3, "client", lambda *_args, **_kwargs: FakeS3Client())
    monkeypatch.setattr(
        _artifacts,
        "wait_for_bucket",
        lambda _client, bucket, endpoint: bucket_waits.append((bucket, endpoint)),
    )
    monkeypatch.setattr(
        _artifacts,
        "assert_ops_artifacts",
        lambda _client, bucket, namespace, _inventory: artifact_checks.append((bucket, namespace)),
    )

    _artifacts.check_ops_artifacts(local_cfg)

    assert bucket_waits == [("custom-ops-bucket", "http://127.0.0.1:19000")]
    assert artifact_checks == [("custom-ops-bucket", "lakehouse")]


def test_assert_ops_artifacts_skips_floe_report_prefix_for_a_product_less_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A domain created via `olf domain new` may have no products yet (see
    the domain-first scaffolding flow); no product job ever selects its
    Silver tables, so Floe never runs for it and it never writes a runtime
    report under floe/reports/<domain>/. Requiring that prefix would fail
    `olf e2e run --env local` for this supported state."""
    product_less_domain = dataclasses.replace(E2E_INVENTORY.domains[0], products=())
    inventory = dataclasses.replace(E2E_INVENTORY, domains=(product_less_domain, *E2E_INVENTORY.domains[1:]))
    checked_prefixes: list[str] = []
    monkeypatch.setattr(_artifacts, "require_s3_prefix", lambda *_args: None)
    monkeypatch.setattr(
        _artifacts, "require_any_s3_prefix", lambda _client, _bucket, prefixes: checked_prefixes.append(prefixes[0])
    )

    _artifacts.assert_ops_artifacts(object(), "ops", "lakehouse", inventory)

    assert product_less_domain.artifact_prefixes.floe_report_prefix not in checked_prefixes
    for domain in inventory.domains:
        if domain.products:
            assert domain.artifact_prefixes.floe_report_prefix in checked_prefixes


def test_assert_ops_artifacts_accepts_floe_reports_under_the_stage_activation_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An activated stage writes Floe reports beneath activations/<stage>/, the
    `olf deploy` path at the bucket root; either proves Floe ran."""

    class ActivatedStageBucket:
        def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int) -> dict[str, Any]:
            return {"Contents": [{"Key": Prefix + "x"}]} if Prefix.startswith("activations/dev/") else {}

    _artifacts.assert_ops_artifacts(ActivatedStageBucket(), "ops", "olf-dev", E2E_INVENTORY)

    with pytest.raises(E2EError, match=r"s3://ops/floe/reports/.* or s3://ops/activations/prod/floe/reports/"):
        _artifacts.assert_ops_artifacts(ActivatedStageBucket(), "ops", "olf-prod", E2E_INVENTORY)
