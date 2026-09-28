import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import E2E_INVENTORY, e2e_cfg

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
        lambda _client, bucket, namespace, _inventory, deployed_revision: artifact_checks.append(
            (bucket, namespace, deployed_revision)
        ),
    )
    monkeypatch.setattr(_artifacts, "deployed_floe_manifest_revision", lambda _cfg: "sha256:" + "a" * 64)

    _artifacts.check_ops_artifacts(local_cfg)

    assert bucket_waits == [("custom-ops-bucket", "http://127.0.0.1:19000")]
    assert artifact_checks == [("custom-ops-bucket", "lakehouse", "sha256:" + "a" * 64)]


def test_deployed_floe_manifest_revision_reads_running_user_code_pods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    deployed_revision = "sha256:" + "a" * 64
    local_cfg = e2e_cfg(tmp_path)
    monkeypatch.setattr(_artifacts, "expected_repository_location_names", lambda _cfg: ["sales", "supply-chain"])
    monkeypatch.setattr(
        _artifacts,
        "expected_user_code_pods",
        lambda _cfg, _locations: [
            "dagster-dagster-user-deployments-sales-abc",
            "dagster-dagster-user-deployments-supply-chain-def",
        ],
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        _artifacts,
        "kubectl",
        lambda _cfg, args, *, capture=False: (
            commands.append(args) or f"PATH=/usr/bin\nOPENLAKEFORGE_FLOE_MANIFEST_REVISION_BUILT={deployed_revision}\n"
        ),
    )

    assert _artifacts.deployed_floe_manifest_revision(local_cfg) == deployed_revision
    assert commands == [
        ["exec", "-n", "lakehouse", "dagster-dagster-user-deployments-sales-abc", "--", "env"],
        ["exec", "-n", "lakehouse", "dagster-dagster-user-deployments-supply-chain-def", "--", "env"],
    ]


def test_deployed_floe_manifest_revision_prefers_the_activated_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`olf project image` bakes "manual"; `olf project deploy` injects the
    stage's activated revision at runtime, and that is the one to verify."""
    activated = "sha256:" + "c" * 64
    monkeypatch.setattr(_artifacts, "expected_repository_location_names", lambda _cfg: ["openlakeforge-dagster"])
    monkeypatch.setattr(_artifacts, "expected_user_code_pods", lambda _cfg, _locations: ["user-code"])
    monkeypatch.setattr(
        _artifacts,
        "kubectl",
        lambda *_args, **_kwargs: (
            f"OPENLAKEFORGE_FLOE_MANIFEST_REVISION_BUILT=manual\nOPENLAKEFORGE_FLOE_MANIFEST_REVISION={activated}\n"
        ),
    )

    assert _artifacts.deployed_floe_manifest_revision(e2e_cfg(tmp_path)) == activated


def test_deployed_floe_manifest_revision_rejects_inconsistent_user_code_pods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = iter(f"OPENLAKEFORGE_FLOE_MANIFEST_REVISION_BUILT=sha256:{c * 64}" for c in "ab")
    monkeypatch.setattr(_artifacts, "expected_repository_location_names", lambda _cfg: ["sales", "supply-chain"])
    monkeypatch.setattr(_artifacts, "expected_user_code_pods", lambda _cfg, _locations: ["sales", "supply-chain"])
    monkeypatch.setattr(_artifacts, "kubectl", lambda *_args, **_kwargs: next(values))

    with pytest.raises(E2EError, match="disagree on the built Floe manifest revision"):
        _artifacts.deployed_floe_manifest_revision(e2e_cfg(tmp_path))


def test_assert_immutable_floe_manifests_verifies_deployed_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    deployed_revision = "sha256:" + "a" * 64

    def verify(_client: Any, bucket: str, revision: str) -> SimpleNamespace:
        assert bucket == "ops"
        assert revision == deployed_revision
        return SimpleNamespace(entries={key: "a" * 64 for key in E2E_INVENTORY.manifest_keys})

    monkeypatch.setattr(_artifacts.revision, "verify", verify)

    _artifacts.assert_immutable_floe_manifests(object(), "ops", E2E_INVENTORY, deployed_revision)


def test_assert_immutable_floe_manifests_requires_all_descriptor_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    deployed_revision = "sha256:" + "a" * 64
    monkeypatch.setattr(
        _artifacts.revision,
        "verify",
        lambda *_args: SimpleNamespace(entries={E2E_INVENTORY.manifest_keys[0]: "a" * 64}),
    )

    with pytest.raises(E2EError, match="every descriptor-discovered domain manifest"):
        _artifacts.assert_immutable_floe_manifests(object(), "ops", E2E_INVENTORY, deployed_revision)


def test_assert_ops_artifacts_uses_legacy_manifests_for_supplied_local_image(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[tuple[str, str]] = []

    class FakeS3Client:
        def head_object(self, *, Bucket: str, Key: str) -> None:
            checked.append((Bucket, Key))

    monkeypatch.setattr(_artifacts, "require_s3_prefix", lambda *_args: None)
    monkeypatch.setattr(_artifacts, "require_any_s3_prefix", lambda *_args: None)
    monkeypatch.setattr(
        _artifacts, "assert_immutable_floe_manifests", lambda *_args: pytest.fail("must use legacy checks")
    )

    _artifacts.assert_ops_artifacts(FakeS3Client(), "ops", "lakehouse", E2E_INVENTORY, "manual")

    assert checked == [("ops", key) for key in E2E_INVENTORY.manifest_keys]


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
    monkeypatch.setattr(_artifacts, "assert_legacy_floe_manifests", lambda *_args: None)
    checked_prefixes: list[str] = []
    monkeypatch.setattr(_artifacts, "require_s3_prefix", lambda *_args: None)
    monkeypatch.setattr(
        _artifacts, "require_any_s3_prefix", lambda _client, _bucket, prefixes: checked_prefixes.append(prefixes[0])
    )

    _artifacts.assert_ops_artifacts(object(), "ops", "lakehouse", inventory, "manual")

    assert product_less_domain.artifact_prefixes.floe_report_prefix not in checked_prefixes
    for domain in inventory.domains:
        if domain.products:
            assert domain.artifact_prefixes.floe_report_prefix in checked_prefixes


def test_assert_ops_artifacts_accepts_floe_reports_under_the_stage_activation_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An activated stage writes Floe reports beneath activations/<stage>/, the
    `olf deploy` path at the bucket root; either proves Floe ran."""
    monkeypatch.setattr(_artifacts, "assert_legacy_floe_manifests", lambda *_args: None)

    class ActivatedStageBucket:
        def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int) -> dict[str, Any]:
            return {"Contents": [{"Key": Prefix + "x"}]} if Prefix.startswith("activations/dev/") else {}

    _artifacts.assert_ops_artifacts(ActivatedStageBucket(), "ops", "olf-dev", E2E_INVENTORY, "manual")

    with pytest.raises(E2EError, match=r"s3://ops/floe/reports/.* or s3://ops/activations/prod/floe/reports/"):
        _artifacts.assert_ops_artifacts(ActivatedStageBucket(), "ops", "olf-prod", E2E_INVENTORY, "manual")
