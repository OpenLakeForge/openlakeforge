import os
from pathlib import Path

import pytest
from conftest import E2E_INVENTORY, E2E_REPO_ROOT, e2e_cfg

from olf.e2e import _runner, _shell
from olf.e2e._shell import E2EError, Environment


@pytest.mark.parametrize("env", ["local", "azure", "aws"])
def test_default_suite_is_full(env: Environment) -> None:
    assert _runner.default_suite(env) == "full"


def test_prepare_config_derives_terraform_roots_from_distribution_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installed distribution's writable project root (bundled demo or
    `--project-root`) is not where its Terraform roots live - those are in
    the read-only payload extracted under `OLF_HOME`. `distribution_root`
    must drive `foundation_terraform_dir`/`contract_terraform_dir`
    independently of `repo_root`, or `olf e2e run` searches an installed
    project root that has no `infra/` tree at all."""
    monkeypatch.delenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", raising=False)
    distribution_root = tmp_path / "distribution"

    cfg = _runner.prepare_config(
        "local",
        suite=None,
        namespace="lakehouse",
        kube_context="",
        repo_root=E2E_REPO_ROOT,
        distribution_root=distribution_root,
    )

    assert cfg.repo_root == E2E_REPO_ROOT
    assert cfg.distribution_root == distribution_root
    assert cfg.foundation_terraform_dir == distribution_root / "infra/terraform/foundations/local-kind"
    assert cfg.contract_terraform_dir == distribution_root / "infra/terraform/environments/local"


def test_prepare_config_distribution_root_defaults_to_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Source-mode checkouts (the pre-#127/#145 behaviour) have one root -
    unset `distribution_root` must fall back to `repo_root` exactly."""
    monkeypatch.delenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", raising=False)

    cfg = _runner.prepare_config(
        "local", suite=None, namespace="lakehouse", kube_context="", repo_root=E2E_REPO_ROOT
    )

    assert cfg.distribution_root == E2E_REPO_ROOT
    assert cfg.foundation_terraform_dir == E2E_REPO_ROOT / "infra/terraform/foundations/local-kind"


def test_prepare_config_uses_external_project_assets_and_distribution_terraform(
    external_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", raising=False)

    cfg = _runner.prepare_config(
        "local",
        suite=None,
        namespace="lakehouse",
        kube_context="",
        repo_root=external_project,
        distribution_root=E2E_REPO_ROOT,
    )

    assert cfg.repo_root == external_project
    assert cfg.distribution_root == E2E_REPO_ROOT
    assert cfg.inventory.products[0].id == "order_revenue"
    assert cfg.contract_terraform_dir == E2E_REPO_ROOT / "infra/terraform/environments/local"


def test_aws_default_suite_includes_smoke_and_full_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    monkeypatch.setattr(_runner, "check_commands", lambda _cfg: None)
    monkeypatch.setattr(_runner, "prepare_kube_context", lambda _cfg: None)
    monkeypatch.setattr(_runner, "check_pods_ready", lambda _cfg: None)
    monkeypatch.setattr(_runner, "run_smoke", lambda _cfg: calls.append("smoke"))
    monkeypatch.setattr(_runner, "run_full", lambda _cfg: calls.append("full"))

    _runner.run("aws", repo_root=E2E_REPO_ROOT)

    assert calls == ["smoke", "full"]


def test_aws_explicit_smoke_suite_skips_full_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    monkeypatch.setattr(_runner, "check_commands", lambda _cfg: None)
    monkeypatch.setattr(_runner, "prepare_kube_context", lambda _cfg: None)
    monkeypatch.setattr(_runner, "check_pods_ready", lambda _cfg: None)
    monkeypatch.setattr(_runner, "run_smoke", lambda _cfg: calls.append("smoke"))
    monkeypatch.setattr(_runner, "run_full", lambda _cfg: calls.append("full"))

    _runner.run("aws", suite="smoke", repo_root=E2E_REPO_ROOT)

    assert calls == ["smoke"]


def test_local_smoke_runs_only_the_descriptor_default_product(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[object] = []
    local_cfg = e2e_cfg(tmp_path, suite="smoke")

    monkeypatch.setattr(_runner, "check_trino_catalog", lambda _cfg: calls.append("catalog"))
    monkeypatch.setattr(_runner, "check_catalog_namespaces", lambda _cfg: calls.append("namespaces"))
    monkeypatch.setattr(
        _runner,
        "launch_and_poll_dagster_jobs",
        lambda _cfg, *, products=None: calls.append(tuple(product.id for product in products or ())),
    )
    monkeypatch.setattr(
        _runner,
        "check_trino_product_tables_and_marts",
        lambda _cfg, product: calls.append(("tables", product.id)),
    )

    _runner.run_smoke(local_cfg)

    assert calls == [
        "catalog",
        "namespaces",
        (E2E_INVENTORY.default_product.id,),
        ("tables", E2E_INVENTORY.default_product.id),
    ]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("local", ["namespaces", "jobs", "tables", "recovery", "superset", "openmetadata", "artifacts", "isolation"]),
        ("azure", ["namespaces", "jobs", "tables", "superset", "openmetadata", "artifacts", "isolation"]),
        ("aws", ["namespaces", "jobs", "tables", "superset", "openmetadata", "artifacts", "isolation"]),
    ],
)
def test_run_full_only_restarts_polaris_for_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: Environment, expected: list[str]
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(_runner, "check_catalog_namespaces", lambda _cfg: calls.append("namespaces"))
    monkeypatch.setattr(_runner, "launch_and_poll_dagster_jobs", lambda _cfg: calls.append("jobs"))
    monkeypatch.setattr(_runner, "check_trino_tables_and_marts", lambda _cfg: calls.append("tables"))
    monkeypatch.setattr(_runner, "check_polaris_restart_recovery", lambda _cfg: calls.append("recovery"))
    monkeypatch.setattr(_runner, "check_superset_dashboards", lambda _cfg: calls.append("superset"))
    monkeypatch.setattr(_runner, "check_openmetadata_assets", lambda _cfg: calls.append("openmetadata"))
    monkeypatch.setattr(_runner, "check_ops_artifacts", lambda _cfg: calls.append("artifacts"))
    monkeypatch.setattr(_runner, "check_stage_isolation", lambda _cfg: calls.append("isolation"))
    monkeypatch.setattr(_runner, "configured_layers", lambda _cfg: {"governance": True, "analytics": True})

    _runner.run_full(e2e_cfg(tmp_path, env=env))

    assert calls == expected


def test_run_full_skips_disabled_layer_assertions_and_reports_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    messages: list[str] = []

    monkeypatch.setattr(_runner, "check_catalog_namespaces", lambda _cfg: calls.append("namespaces"))
    monkeypatch.setattr(_runner, "launch_and_poll_dagster_jobs", lambda _cfg: calls.append("jobs"))
    monkeypatch.setattr(_runner, "check_trino_tables_and_marts", lambda _cfg: calls.append("tables"))
    monkeypatch.setattr(_runner, "check_polaris_restart_recovery", lambda _cfg: calls.append("recovery"))
    monkeypatch.setattr(_runner, "check_superset_dashboards", lambda _cfg: calls.append("superset"))
    monkeypatch.setattr(_runner, "check_openmetadata_assets", lambda _cfg: calls.append("openmetadata"))
    monkeypatch.setattr(_runner, "check_ops_artifacts", lambda _cfg: calls.append("artifacts"))
    monkeypatch.setattr(_runner, "check_stage_isolation", lambda _cfg: calls.append("isolation"))
    monkeypatch.setattr(_runner, "configured_layers", lambda _cfg: {"governance": False, "analytics": False})
    monkeypatch.setattr(_runner.log, "info", messages.append)

    _runner.run_full(e2e_cfg(tmp_path))

    assert calls == ["namespaces", "jobs", "tables", "recovery", "artifacts", "isolation"]
    assert "Skipped e2e assertions: Superset dashboards, OpenMetadata governance assets" in messages


def test_prepare_kube_context_refreshes_existing_aws_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    kubeconfig = tmp_path / "aws.yaml"
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))

    def run(args: list[str], *, capture: bool = False) -> str:
        commands.append(args)
        return ""

    # prepare_kube_context calls `_run` directly (bound in _runner); its final
    # `_run_retry(...)` call resolves `_run` from _shell's own namespace, so
    # both bindings need the same fake to capture every command.
    monkeypatch.setattr(_runner, "_run", run)
    monkeypatch.setattr(_shell, "_run", run)
    monkeypatch.setattr(_runner, "_kubectl_executable", lambda: "kubectl")
    monkeypatch.setattr(
        _runner,
        "terraform_output",
        lambda _dir, name: {
            "aws_region": "eu-west-1",
            "cluster_name": "limited-eks-openlakeforge-poc",
        }[name],
    )
    sdk_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "olf.tooling.aws.AwsSdk.eks_update_kubeconfig",
        lambda _self, *args, **kwargs: sdk_calls.append((args, kwargs)),
    )

    _runner.prepare_kube_context(e2e_cfg(tmp_path, env="aws", suite="smoke"))

    assert ["kubectl", "cluster-info", "--context", "kind-openlakeforge-local"] in commands
    assert sdk_calls == [
        (
            ("limited-eks-openlakeforge-poc",),
            {
                "region": "eu-west-1",
                "kubeconfig_path": kubeconfig,
                "alias": "kind-openlakeforge-local",
            },
        )
    ]


@pytest.mark.parametrize(
    ("env", "terraform_outputs"),
    [
        (
            "azure",
            {"resource_group_name": "rg-sandbox", "cluster_name": "aks-openlakeforge-poc"},
        ),
        (
            "aws",
            {"aws_region": "eu-west-1", "cluster_name": "limited-eks-openlakeforge-poc"},
        ),
    ],
)
def test_prepare_kube_context_uses_provider_default_for_direct_cloud_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: Environment,
    terraform_outputs: dict[str, str],
) -> None:
    commands: list[list[str]] = []
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv(f"{env.upper()}_KUBECONFIG_PATH", raising=False)
    run = lambda args, capture=False: commands.append(args) or ""  # noqa: E731
    monkeypatch.setattr(_runner, "_run", run)
    monkeypatch.setattr(_shell, "_run", run)
    monkeypatch.setattr(_runner, "_kubectl_executable", lambda: "kubectl")
    monkeypatch.setattr(_runner, "terraform_output", lambda _dir, name: terraform_outputs[name])
    sdk_calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        "olf.tooling.azure.AzureSdk.aks_get_credentials",
        lambda _self, *args, **kwargs: sdk_calls.append(("azure", args, kwargs)),
    )
    monkeypatch.setattr(
        "olf.tooling.aws.AwsSdk.eks_update_kubeconfig",
        lambda _self, *args, **kwargs: sdk_calls.append(("aws", args, kwargs)),
    )

    _runner.prepare_kube_context(e2e_cfg(tmp_path, env=env, suite="smoke"))

    expected_kubeconfig = tmp_path / ".tmp/kubeconfigs" / f"{env}.yaml"
    assert sdk_calls[0][0] == env
    assert sdk_calls[0][2]["kubeconfig_path"] == expected_kubeconfig
    assert Path(os.environ["KUBECONFIG"]) == expected_kubeconfig
    assert expected_kubeconfig.parent.is_dir()


def test_prepare_kube_context_selects_existing_local_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def run(args: list[str], *, capture: bool = False) -> str:
        commands.append(args)
        return ""

    monkeypatch.setattr(_runner, "_run", run)
    monkeypatch.setattr(_runner, "_kubectl_executable", lambda: "kubectl")

    _runner.prepare_kube_context(e2e_cfg(tmp_path))

    assert ["kubectl", "cluster-info", "--context", "kind-openlakeforge-local"] in commands
    assert not any(command[:3] == ["kubectl", "config", "use-context"] for command in commands)


def test_prepare_kube_context_updates_aws_context_when_existing_context_is_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    kubeconfig = tmp_path / "aws.yaml"
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))

    def run(args: list[str], *, capture: bool = False) -> str:
        commands.append(args)
        if args[:2] == ["kubectl", "cluster-info"] and len(commands) == 1:
            raise E2EError("context missing")
        return ""

    monkeypatch.setattr(_runner, "_run", run)
    monkeypatch.setattr(_shell, "_run", run)
    monkeypatch.setattr(_shell.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(_runner, "_kubectl_executable", lambda: "kubectl")
    monkeypatch.setattr(
        _runner,
        "terraform_output",
        lambda _dir, name: {
            "aws_region": "eu-west-1",
            "cluster_name": "limited-eks-openlakeforge-poc",
        }[name],
    )
    sdk_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "olf.tooling.aws.AwsSdk.eks_update_kubeconfig",
        lambda _self, *args, **kwargs: sdk_calls.append((args, kwargs)),
    )

    _runner.prepare_kube_context(e2e_cfg(tmp_path, env="aws", suite="smoke"))

    assert sdk_calls == [
        (
            ("limited-eks-openlakeforge-poc",),
            {"region": "eu-west-1", "kubeconfig_path": kubeconfig, "alias": "kind-openlakeforge-local"},
        )
    ]


def test_terraform_output_honors_external_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installed distribution's AWS/Azure foundation state lives under
    OLF_HOME (OPENLAKEFORGE_TERRAFORM_STATE_ROOT/_DATA_ROOT), not next to
    the foundation Terraform directory - terraform_output must translate
    that the same way `Terraform._run` does, or every foundation read
    (region, cluster name) silently targets the payload's absent state."""
    captured: dict = {}

    def run(args: list[str], *, capture: bool = False, env=None) -> str:  # noqa: ANN001
        captured["args"] = args
        captured["env"] = env
        return "us-east-1"

    monkeypatch.setattr(_shell, "_run", run)
    monkeypatch.setattr(_shell, "_terraform_executable", lambda: "terraform")
    monkeypatch.setenv("OPENLAKEFORGE_TERRAFORM_DATA_ROOT", str(tmp_path / "terraform-data"))
    monkeypatch.setenv("OPENLAKEFORGE_TERRAFORM_STATE_ROOT", str(tmp_path / "state"))

    foundation_dir = tmp_path / "foundations" / "aws-eks"
    assert _shell.terraform_output(foundation_dir, "aws_region") == "us-east-1"

    expected_state = tmp_path / "state" / "foundation.tfstate"
    assert f"-state={expected_state}" in captured["args"]
    assert not expected_state.parent.exists(), "a read-only output call must not create state directories"
    assert captured["env"]["TF_DATA_DIR"] == str(tmp_path / "terraform-data" / "foundation")


def test_terraform_output_without_external_state_root_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}

    def run(args: list[str], *, capture: bool = False, env=None) -> str:  # noqa: ANN001
        captured["args"] = args
        captured["env"] = env
        return "us-east-1"

    monkeypatch.setattr(_shell, "_run", run)
    monkeypatch.setattr(_shell, "_terraform_executable", lambda: "terraform")

    foundation_dir = tmp_path / "foundations" / "aws-eks"
    assert _shell.terraform_output(foundation_dir, "aws_region") == "us-east-1"

    assert captured["args"] == ["terraform", f"-chdir={foundation_dir}", "output", "-raw", "aws_region"]
    assert captured["env"] is None


def test_run_retry_retries_transient_command_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def run(args: list[str], *, capture: bool = False) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise E2EError("temporary failure")
        return "ok"

    monkeypatch.setattr(_shell, "_run", run)
    monkeypatch.setattr(_shell.time, "sleep", lambda _delay: None)

    assert _shell._run_retry(["kubectl", "cluster-info"], capture=True, attempts=2, delay=0) == "ok"
    assert attempts == 2


def test_run_retry_transient_kubectl_retries_tls_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def run(args: list[str], *, capture: bool = False) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise E2EError("Unable to connect to the server: net/http: TLS handshake timeout")
        return "6"

    monkeypatch.setattr(_shell, "_run", run)
    monkeypatch.setattr(_shell.time, "sleep", lambda _delay: None)

    assert _shell._run_retry_transient_kubectl(["kubectl", "exec"], attempts=3) == "6"
    assert attempts == 2


def test_run_retry_transient_kubectl_does_not_retry_query_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def run(args: list[str], *, capture: bool = False) -> str:
        nonlocal attempts
        attempts += 1
        raise E2EError("Trino query failed: TABLE_NOT_FOUND")

    monkeypatch.setattr(_shell, "_run", run)

    with pytest.raises(E2EError, match="TABLE_NOT_FOUND"):
        _shell._run_retry_transient_kubectl(["kubectl", "exec"], attempts=3)
    assert attempts == 1


def test_check_commands_translates_a_toolchain_error_into_an_e2e_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A managed tool that fails to provision (bad digest, broken download,
    unwritable cache) must surface as E2EError - the only exception type
    `olf e2e run` catches - not escape as a raw ToolchainError traceback."""
    from olf.deployment.errors import ToolchainError

    class _FailingResolver:
        def resolve(self, tool: str) -> Path:
            if tool == "terraform":
                raise ToolchainError(tool, reason="digest mismatch")
            return Path(f"/usr/bin/{tool}")

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda: _FailingResolver())

    with pytest.raises(E2EError, match="digest mismatch"):
        _runner.check_commands(e2e_cfg(tmp_path))
