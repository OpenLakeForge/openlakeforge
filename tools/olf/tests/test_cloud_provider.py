"""`CloudProvider` orchestration tests.

The facts-before-env ordering here is the single highest-risk detail in the
AWS/Azure port: `DeploymentContext.kube_context` is unknown until the
foundation's Terraform outputs are read, unlike local's static
`kind-<cluster>`. `env` must resolve `foundation_facts` before building the
command environment, or `KUBE_CONTEXT` gets baked in empty.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from _cloud_support import FakeCloudBackend

from olf.deployment.cloud.aws import AwsBackend
from olf.deployment.cloud.azure import AzureBackend
from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.cloud.provider import CloudProvider
from olf.deployment.context import DeploymentContext
from olf.deployment.engine import DeploymentPhase, Toolkit
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.status import StatusReport
from olf.tooling.azure import AzureSdk

_FACTS = FoundationFacts(
    cluster_name="eks-openlakeforge-poc",
    kube_context="eks-openlakeforge-poc",
    project_code_repository="123.dkr.ecr.eu-west-1.amazonaws.com/project-code",
    superset_repository="123.dkr.ecr.eu-west-1.amazonaws.com/superset",
    aws_region="eu-west-1",
)


def _toolkit() -> Toolkit:
    return Toolkit.default(overrides={t: Path(t) for t in ("terraform", "docker", "kind", "kubectl", "helm")})


def _config(tmp_path: Path) -> CloudDeploymentConfig:
    context = DeploymentContext.aws(repo_root=tmp_path)
    return CloudDeploymentConfig.from_environment({}, context=context)


def test_status_reports_every_owned_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.deployment.context import Provider
    from olf.profile import (
        DeploymentProfile,
        Preset,
        ProviderSpec,
        StageCapabilities,
        StageName,
        StageSpec,
        resolve_topology,
    )

    topology = resolve_topology(
        DeploymentProfile(
            name="acme-data",
            provider=ProviderSpec(type=Provider.AWS),
            preset=Preset.SLIM,
            stages=(
                StageSpec(name=StageName.DEV, capabilities=StageCapabilities()),
                StageSpec(name=StageName.PROD, capabilities=StageCapabilities()),
            ),
        )
    )
    context = DeploymentContext.aws(repo_root=tmp_path, topology=topology)
    config = CloudDeploymentConfig.from_environment({}, context=context)
    provider = CloudProvider.create(config, FakeCloudBackend(scope="aws", facts=_FACTS), toolkit=_toolkit(), environ={})
    provider.__dict__["foundation_facts"] = _FACTS
    provider.__dict__["env"] = {}
    captured: dict[str, object] = {}

    def _collect_status(_kubectl, *, namespaces, **_kwargs):  # noqa: ANN001
        captured["namespaces"] = namespaces
        return StatusReport(sections=())

    monkeypatch.setattr("olf.deployment.status.collect_status", _collect_status)

    provider.status()

    assert captured["namespaces"] == ("olf-system", "olf-dev", "olf-prod")


def test_env_raises_before_foundation_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws")
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})

    with pytest.raises(DeploymentPreconditionError, match="foundation Terraform state is missing"):
        _ = provider.env


def test_env_resolves_kube_context_from_foundation_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    monkeypatch.setattr(
        "olf.deployment.cloud.foundation.require_foundation_facts", lambda cfg, tools, be, *, env: _FACTS
    )

    resolved_env = provider.env

    assert resolved_env["KUBE_CONTEXT"] == _FACTS.kube_context


def test_base_environment_preserves_automation_credentials_for_sdk_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(
        config,
        backend,
        toolkit=_toolkit(),
        environ={"AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/token", "AWS_ROLE_ARN": "arn:aws:iam::123:role/ci"},
    )
    monkeypatch.setattr(provider.tools.docker, "resolve_current_engine_endpoint", lambda **_kwargs: None)
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        "olf.auth.terraform_auth_environment",
        lambda _provider, env: seen.update(env) or {},
    )

    _ = provider._base_env

    assert seen["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/tmp/token"
    assert seen["AWS_ROLE_ARN"] == "arn:aws:iam::123:role/ci"


def test_doctor_uses_the_selected_authentication_environment(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(
        config,
        backend,
        toolkit=_toolkit(),
        environ={"OLF_HOME": "/custom/olf", "AWS_PROFILE": "company-sso"},
    )
    seen: dict[str, str] = {}
    backend.preflight = lambda _tools, *, env: seen.update(env)  # type: ignore[method-assign]

    provider.doctor(DeploymentPhase.FOUNDATION)

    assert seen["OLF_HOME"] == "/custom/olf"
    assert seen["AWS_PROFILE"] == "company-sso"


def test_doctor_reports_azure_authenticated_after_login_with_multiple_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`doctor()` builds its preflight environment from
    `credential_selection_environment` alone - no `ARM_SUBSCRIPTION_ID`
    overlay from Terraform's authentication environment. An identity with
    several subscriptions and only saved `olf auth login` state must still
    report authenticated instead of "Azure subscription is not selected".
    """
    context = DeploymentContext.azure(repo_root=tmp_path)
    config = CloudDeploymentConfig.from_environment({}, context=context)
    backend = AzureBackend()
    subscriptions = [
        SimpleNamespace(subscription_id="sub-id", display_name="One", tenant_id="tenant", state="Enabled"),
        SimpleNamespace(subscription_id="other-sub-id", display_name="Two", tenant_id="tenant", state="Enabled"),
    ]
    azure_sdk = AzureSdk(
        credential=SimpleNamespace(get_token=lambda *_args: SimpleNamespace(token="arm-token")),
        subscription_client_factory=lambda _credential: SimpleNamespace(
            subscriptions=SimpleNamespace(list=lambda: subscriptions)
        ),
    )
    toolkit = replace(_toolkit(), azure=azure_sdk)
    provider = CloudProvider.create(config, backend, toolkit=toolkit, environ={"OLF_HOME": str(tmp_path)})
    monkeypatch.setattr("olf.auth.selected_azure_subscription", lambda _environ: "other-sub-id")

    report = provider.doctor(DeploymentPhase.FOUNDATION)

    auth_item = next(item for item in report.items if item.name == "azure authentication")
    assert auth_item.ok is True


def test_env_is_cached_and_facts_are_resolved_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    calls: list[int] = []

    def _fake_require(cfg, tools, be, *, env):  # noqa: ANN001, ARG001
        calls.append(1)
        return _FACTS

    monkeypatch.setattr("olf.deployment.cloud.foundation.require_foundation_facts", _fake_require)

    _ = provider.env
    _ = provider.env

    assert len(calls) == 1


def test_foundation_up_does_not_require_pre_existing_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`foundation_up` must use `_base_env`, not `env` - there is no foundation yet to derive a
    kube_context from, so reading `env` here would raise before the foundation exists.
    """
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    calls: list[str] = []

    def _fake_foundation_up(cfg, tools, be, *, environ, env):  # noqa: ANN001, ARG001
        calls.append("foundation_up")
        return _FACTS

    monkeypatch.setattr("olf.deployment.cloud.foundation.foundation_up", _fake_foundation_up)

    provider.foundation_up()

    assert calls == ["foundation_up"]


def test_platform_up_after_foundation_up_resolves_fresh_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact sequence `DeploymentEngine.deploy()` (phase=ALL) exercises on one provider
    instance: foundation_up() then platform_up(), with platform_up() needing the
    just-created foundation's facts.
    """
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})

    monkeypatch.setattr(
        "olf.deployment.cloud.foundation.foundation_up",
        lambda cfg, tools, be, *, environ, env: _FACTS,  # noqa: ARG005
    )
    monkeypatch.setattr(
        "olf.deployment.cloud.foundation.require_foundation_facts",
        lambda cfg, tools, be, *, env: _FACTS,  # noqa: ARG005
    )
    seen_facts = []
    monkeypatch.setattr(
        "olf.deployment.cloud.platform.platform_up",
        lambda cfg, tools, be, facts, *, env: seen_facts.append(facts),  # noqa: ARG005
    )

    provider.foundation_up()
    provider.platform_up()

    assert seen_facts == [_FACTS]


def test_prepare_images_is_a_no_op(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws")
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})

    provider.prepare_images()  # must not raise, must not touch the foundation


def test_platform_plan_prepares_cached_charts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    calls: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.cloud.foundation.require_foundation_facts", lambda cfg, tools, be, *, env: _FACTS
    )
    monkeypatch.setattr(
        "olf.deployment.cloud.platform.prepare_charts", lambda *args, **kwargs: calls.append("charts")
    )
    monkeypatch.setattr(provider.tools.terraform, "init", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        provider.tools.terraform,
        "plan",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    provider.plan(DeploymentPhase.PLATFORM)

    assert calls == ["charts"]


def test_foundation_doctor_does_not_require_or_probe_later_phase_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    required: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.cloud.provider.base_report",
        lambda **kwargs: required.extend(kwargs["required_tools"]) or [],
    )
    monkeypatch.setattr("olf.deployment.cloud.provider.docker_health", lambda *args, **kwargs: pytest.fail("no docker"))

    provider.doctor(DeploymentPhase.FOUNDATION)

    assert required == ["terraform", "kubectl"]


def test_artifacts_doctor_requires_and_probes_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    required: list[str] = []
    health_calls: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.cloud.provider.base_report",
        lambda **kwargs: required.extend(kwargs["required_tools"]) or [],
    )
    monkeypatch.setattr(
        "olf.deployment.cloud.provider.docker_health",
        lambda *args, **kwargs: health_calls.append("docker") or None,
    )
    monkeypatch.setattr("olf.contracts.load_provider_contracts", lambda *_args, **_kwargs: None)

    provider.doctor(DeploymentPhase.ARTIFACTS)

    assert required == ["terraform", "kubectl", "docker"]
    assert health_calls == ["docker"]


def test_cloud_artifacts_doctor_requires_platform_provider_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    monkeypatch.setattr("olf.contracts.load_provider_contracts", lambda *_args, **_kwargs: None)

    report = provider.doctor(DeploymentPhase.ARTIFACTS)

    contracts_item = next(item for item in report.items if item.name == "aws platform provider contracts")
    assert contracts_item.ok is False


def test_full_platform_doctor_requires_and_probes_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    required: list[str] = []
    health_calls: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.cloud.provider.base_report",
        lambda **kwargs: required.extend(kwargs["required_tools"]) or [],
    )
    monkeypatch.setattr(
        "olf.deployment.cloud.provider.docker_health",
        lambda *args, **kwargs: health_calls.append("docker") or None,
    )

    provider.doctor(DeploymentPhase.PLATFORM)

    assert required == ["terraform", "kubectl", "helm", "docker"]
    assert health_calls == ["docker"]


def test_platform_doctor_requires_docker_when_a_non_selected_stage_has_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors platform_up()'s own aggregate capability check: a slim selected
    DEV stage must not hide the Docker requirement/health check for a
    platform apply that builds Superset because some other enabled stage
    (PROD here) turned analytics on."""
    from olf.deployment.context import Provider
    from olf.profile import (
        DeploymentProfile,
        Preset,
        ProviderSpec,
        StageCapabilities,
        StageName,
        StageSpec,
        resolve_topology,
    )

    topology = resolve_topology(
        DeploymentProfile(
            name="acme-data",
            provider=ProviderSpec(type=Provider.AWS),
            preset=Preset.SLIM,
            stages=(
                StageSpec(name=StageName.DEV, capabilities=StageCapabilities()),
                StageSpec(name=StageName.PROD, capabilities=StageCapabilities(analytics=True, governance=True)),
            ),
        )
    )
    context = DeploymentContext.aws(repo_root=tmp_path, topology=topology)
    config = CloudDeploymentConfig.from_environment({}, context=context)
    assert config.features.analytics_enabled is False
    assert config.context.platform_features.analytics_enabled is True
    provider = CloudProvider.create(config, FakeCloudBackend(scope="aws", facts=_FACTS), toolkit=_toolkit(), environ={})
    required: list[str] = []
    monkeypatch.setattr(
        "olf.deployment.cloud.provider.base_report",
        lambda **kwargs: required.extend(kwargs["required_tools"]) or [],
    )
    monkeypatch.setattr("olf.deployment.cloud.provider.docker_health", lambda *args, **kwargs: None)

    provider.doctor(DeploymentPhase.PLATFORM)

    assert "docker" in required


def test_azure_foundation_doctor_reports_missing_required_tfvars(tmp_path: Path) -> None:
    context = DeploymentContext.azure(repo_root=tmp_path)
    config = CloudDeploymentConfig.from_environment({}, context=context)
    provider = CloudProvider.create(config, AzureBackend(), toolkit=_toolkit(), environ={})

    report = provider.doctor(DeploymentPhase.FOUNDATION)

    tfvars_item = next(item for item in report.items if item.name == "azure foundation tfvars")
    assert not tfvars_item.ok
    assert "Azure foundation configuration not found" in tfvars_item.detail


def test_cloud_doctor_reports_missing_explicit_tfvars_for_each_consuming_phase(tmp_path: Path) -> None:
    context = DeploymentContext.aws(repo_root=tmp_path)
    environ = {"AWS_TFVARS_FILE": "missing.tfvars"}
    config = CloudDeploymentConfig.from_environment(environ, context=context)
    provider = CloudProvider.create(config, AwsBackend(), toolkit=_toolkit(), environ=environ)

    report = provider.doctor(DeploymentPhase.ALL)

    tfvars_items = {item.name: item for item in report.items if item.name.endswith("tfvars")}
    assert not tfvars_items["aws foundation tfvars"].ok
    assert not tfvars_items["aws platform tfvars"].ok


def test_local_platform_doctor_reports_missing_explicit_tfvars(tmp_path: Path) -> None:
    from olf.deployment.local.config import LocalDeploymentConfig
    from olf.deployment.local.provider import LocalProvider

    context = DeploymentContext.local(repo_root=tmp_path)
    config = LocalDeploymentConfig.from_environment({"LOCAL_TFVARS_FILE": "missing.tfvars"}, context=context)
    provider = LocalProvider.create(config, toolkit=_toolkit(), environ={})

    report = provider.doctor(DeploymentPhase.PLATFORM)

    tfvars_item = next(item for item in report.items if item.name == "local platform tfvars")
    assert not tfvars_item.ok


def test_doctor_resolves_the_active_docker_context_for_the_engine_check(tmp_path: Path) -> None:
    """`command_env` scopes DOCKER_CONFIG to an OLF-owned directory, which
    drops the user's `currentContext`. Without resolving the active engine
    endpoint, `docker version` falls back to the default context's socket and
    doctor reports a colima/Rancher/remote engine as unreachable even though
    `olf deploy` (via `_base_env`) drives it perfectly well.
    """
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(config, backend, toolkit=_toolkit(), environ={})
    seen: dict[str, str] = {}
    provider.tools.docker.resolve_current_engine_endpoint = (  # type: ignore[method-assign]
        lambda **_kwargs: "unix:///Users/dev/.colima/default/docker.sock"
    )
    provider.tools.docker.version = lambda *, env: seen.update(env) or SimpleNamespace(  # type: ignore[method-assign]
        ok=True, stdout="Docker Engine", stderr=""
    )

    provider.doctor(DeploymentPhase.ARTIFACTS)

    assert seen["DOCKER_HOST"] == "unix:///Users/dev/.colima/default/docker.sock"


def test_doctor_keeps_an_explicit_docker_host_override(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", facts=_FACTS)
    provider = CloudProvider.create(
        config, backend, toolkit=_toolkit(), environ={"DOCKER_HOST": "tcp://explicit:2375"}
    )
    resolved: list[str] = []
    provider.tools.docker.resolve_current_engine_endpoint = (  # type: ignore[method-assign]
        lambda **_kwargs: resolved.append("called") or "unix:///should/not/be/used.sock"
    )
    provider.tools.docker.version = lambda *, env: SimpleNamespace(  # type: ignore[method-assign]
        ok=True, stdout="Docker Engine", stderr=""
    )

    provider.doctor(DeploymentPhase.ARTIFACTS)

    assert resolved == [], "an explicit DOCKER_HOST must not be overridden by context detection"


def test_doctor_reads_contracts_through_the_toolkit_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scoped `environ` lacks OLF_TOOLCHAIN_MODE, so a resolver rebuilt from
    it selects managed provisioning and reaches the network (#233)."""

    def _no_rebuild(**_kwargs: object) -> None:
        raise AssertionError("doctor must not build a second resolver")

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", _no_rebuild)
    provider = CloudProvider.create(_config(tmp_path), AwsBackend(), toolkit=_toolkit(), environ={})

    report = provider.doctor(DeploymentPhase.ALL)

    assert any(item.name == "aws platform provider contracts" for item in report.items)
