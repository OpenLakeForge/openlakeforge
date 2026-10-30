from __future__ import annotations

import os

import pytest

from olf.infra import smoke


@pytest.fixture(autouse=True)
def _no_real_contract_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """`applied_contract_environment` shells out to `terraform output`; every
    test here mocks the contracts module so no real subprocess runs (mirrors
    test_cli_e2e.py's `_no_real_contract_resolution`)."""
    from olf.infra import contracts as contracts_module

    monkeypatch.setattr(
        "olf.common.contract_state.load_provider_contracts",
        lambda terraform_dir, *, environ=None: None,
    )
    monkeypatch.setattr(
        contracts_module, "build_contract_env", lambda base, contracts_value, *, repo_root, **_: ({}, [])
    )


def test_run_uses_deployment_engine_and_e2e_without_make(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    calls: list[object] = []
    monkeypatch.setattr(smoke.config, "repo_root", lambda: tmp_path)
    contexts: list[object] = []
    monkeypatch.setattr(smoke, "build_provider", lambda context, *args, **kwargs: contexts.append(context) or object())
    monkeypatch.setattr(smoke.DeploymentEngine, "deploy", lambda _self, phase: calls.append(phase))
    monkeypatch.setattr(smoke.e2e, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    smoke.run(
        timeout_seconds=2700,
        environ={
            "CLUSTER_NAME": "openlakeforge-pr-123",
            "LOCAL_KUBECONFIG_PATH": str(tmp_path / "ci.yaml"),
            # tmp_path has no release/component-catalog.yaml; managed
            # toolchain resolution is exercised separately in
            # tests/common/test_toolchain_resolver.py.
            "OLF_TOOLCHAIN_MODE": "host",
        },
        monotonic=iter((0.0, 1.0, 2.0, 3.0)).__next__,
    )

    assert calls[0] is smoke.DeploymentPhase.ALL
    assert calls[1][0] == ("local",)
    assert contexts[0].kube_context == "kind-openlakeforge-pr-123"
    assert calls[1][1]["kube_context"] == "kind-openlakeforge-pr-123"


def test_run_derives_its_namespaces_from_the_topology(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """A NAMESPACE override cannot move what the local root creates, so the
    smoke suite must validate the namespaces the deploy actually made."""
    contexts: list[object] = []
    e2e_calls: list[dict] = []
    monkeypatch.setattr(smoke.config, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(smoke, "build_provider", lambda context, *args, **kwargs: contexts.append(context) or object())
    monkeypatch.setattr(smoke.DeploymentEngine, "deploy", lambda *args: None)
    monkeypatch.setattr(smoke.e2e, "run", lambda *args, **kwargs: e2e_calls.append(kwargs))

    smoke.run(
        timeout_seconds=2700,
        environ={"OPENLAKEFORGE_KUBE_NAMESPACE": "custom-lakehouse", "OLF_TOOLCHAIN_MODE": "host"},
        monotonic=iter((0.0, 1.0, 2.0, 3.0)).__next__,
    )

    assert contexts[0].namespace == "olf-dev"
    assert e2e_calls[0]["namespace"] == "olf-dev"


def test_run_tells_e2e_where_the_shared_services_live(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """Trino, Polaris, and SeaweedFS are deployed once, outside the stage
    namespace. Without the shared namespace the suite looks for them beside
    Dagster and fails with `deployments.apps "trino-coordinator" not found`."""
    e2e_calls: list[dict] = []
    monkeypatch.setattr(smoke.config, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(smoke, "build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke.DeploymentEngine, "deploy", lambda *args: None)
    monkeypatch.setattr(smoke.e2e, "run", lambda *args, **kwargs: e2e_calls.append(kwargs))

    smoke.run(
        timeout_seconds=2700,
        environ={"OLF_TOOLCHAIN_MODE": "host"},
        monotonic=iter((0.0, 1.0, 2.0, 3.0)).__next__,
    )

    assert e2e_calls[0]["namespace"] == "olf-dev"
    assert e2e_calls[0]["shared_namespace"] == "olf-system"


def test_run_exports_the_provider_contract_environment_before_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """e2e.run() must see the same contract-derived exports the CLI's own
    `olf e2e run` applies (e.g. OPENLAKEFORGE_QUERY_TRINO_CATALOG) - without
    them, Trino catalog lookups silently fall back to the stale "iceberg"
    default instead of this profile's actual stage catalog name."""
    from olf.infra import contracts as contracts_module

    monkeypatch.setattr(
        contracts_module,
        "build_contract_env",
        lambda base, contracts_value, *, repo_root, **_: ({"OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_dev"}, []),
    )
    observed: dict[str, str | None] = {}
    monkeypatch.setattr(smoke.config, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(smoke, "build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke.DeploymentEngine, "deploy", lambda *args: None)
    def _capture_catalog(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        observed["catalog"] = os.environ.get("OPENLAKEFORGE_QUERY_TRINO_CATALOG")

    monkeypatch.setattr(smoke.e2e, "run", _capture_catalog)

    smoke.run(
        timeout_seconds=2700,
        environ={"OLF_TOOLCHAIN_MODE": "host"},
        monotonic=iter((0.0, 1.0, 2.0, 3.0)).__next__,
    )

    assert observed["catalog"] == "lakehouse_dev"


def test_run_applies_the_supplied_environment_during_deployment_and_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    deployment_observed: dict[str, str | None] = {}
    e2e_observed: dict[str, str | None] = {}
    supplied = {
        "LOCAL_KUBECONFIG_PATH": str(tmp_path / "custom-kubeconfig.yaml"),
        "KUBECONFIG": str(tmp_path / "explicit-kubeconfig.yaml"),
        "OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR": str(tmp_path / "custom-contracts"),
    }
    monkeypatch.setattr(smoke.config, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(smoke, "build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        smoke.DeploymentEngine,
        "deploy",
        lambda *args: deployment_observed.update({key: os.environ.get(key) for key in supplied}),
    )
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", raising=False)

    def _capture_e2e(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        e2e_observed.update({key: os.environ.get(key) for key in supplied})

    monkeypatch.setattr(smoke.e2e, "run", _capture_e2e)

    smoke.run(timeout_seconds=2700, environ=supplied, monotonic=iter((0.0, 1.0, 2.0, 3.0)).__next__)

    assert deployment_observed == supplied
    assert e2e_observed == supplied
    assert "KUBECONFIG" not in os.environ
    assert "OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR" not in os.environ


def test_run_rejects_a_nonpositive_budget() -> None:
    with pytest.raises(smoke.SmokeError, match="greater than zero"):
        smoke.run(timeout_seconds=0)


def test_deadline_interrupts_blocking_work(monkeypatch: pytest.MonkeyPatch) -> None:
    class Signal:
        SIGALRM = 14
        ITIMER_REAL = 0
        handler = None
        timer_calls: list[tuple[object, ...]] = []

        @classmethod
        def getsignal(cls, _signal: int):  # noqa: ANN206
            return "previous"

        @classmethod
        def setitimer(cls, *args: object) -> tuple[int, int]:
            cls.timer_calls.append(args)
            return (0, 0)

        @classmethod
        def signal(cls, _signal: int, handler: object) -> None:
            cls.handler = handler

    monkeypatch.setattr(smoke, "signal", Signal)

    with pytest.raises(smoke.SmokeError, match="time budget"):
        with smoke._deadline(1):
            assert Signal.handler is not None
            Signal.handler(14, None)

    assert Signal.timer_calls[-1] == (0, 0, 0)
