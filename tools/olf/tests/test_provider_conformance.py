"""The contract every provider adapter must satisfy, asserted once per provider.

The per-module tests next to this one each cover one adapter. Nothing before
this suite asserted that *every* adapter satisfies the same contract, so an
adapter could diverge and only fail at runtime, on the provider nobody
exercises locally (issue #188).

The parameter list is `olf.profile.Provider` itself rather than a
literal list, so a provider is enrolled the moment the enum names it: adding
one fails here — with a message naming the contract it broke — until its
`DeploymentContext` factory, its `build_provider` branch, its captured
contract fixture, and its Terraform root all exist.

Runs with no cluster and no cloud credentials: every provider is built from an
empty environment, every subprocess goes to a recording runner, and the
provider contract each root emits is read from the captured fixture beside
this file. What that leaves uncovered is one thing only — that a real
`terraform apply` of a root emits exactly the fixture's payload. That needs
Terraform and a provider account, and stays with `olf e2e run`. The two static
checks here (the root declares a v3 stage-indexed contract surface, and the
outputs shared tooling reads) are what catch a root drifting away from its
fixture in the meantime -- as presence checks over an opaque expression
string, not as proof that each field is pinned to its own stage.

Assertions are written against what a caller can observe. The destructive-apply
guards are driven through each provider's platform apply rather than called
directly, so a provider whose apply stops invoking them fails here; the
Protocol check binds Protocol-shaped calls rather than comparing signature
text, so an adapter may be rewritten freely as long as shared callers still
work; and cross-provider equality covers only the logical identities olf itself
derives, never physical names the provider contract owns.
"""

from __future__ import annotations

import copy
import inspect
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import hcl2
import pytest
from _cloud_support import FakeCloudBackend
from _contract_conformance import (
    ACTIVATION_URIS as _ACTIVATION_URIS,
)
from _contract_conformance import (
    CAPABILITY_GATED_EXPORTS as _CAPABILITY_GATED_EXPORTS,
)
from _contract_conformance import (
    MEDALLION_LAYERS as _MEDALLION_LAYERS,
)
from _contract_conformance import (
    STAGE_BINDINGS as _STAGE_BINDINGS,
)
from _contract_conformance import (
    STAGE_DERIVED_EXPORTS as _STAGE_DERIVED_EXPORTS,
)
from _contract_conformance import (
    logical_identities as _logical_identities,
)
from _contract_conformance import (
    names_stage as _names_stage,
)
from _tooling_support import RecordedCall, RecordingRunner
from conftest import CONFORMANCE_STAGES, write_two_product_fixture

from olf.contracts import build_contract_env
from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.context import DeploymentContext
from olf.deployment.engine import (
    DeploymentConfig,
    DeploymentEngine,
    DeploymentPhase,
    DeploymentProvider,
    build_provider,
)
from olf.deployment.errors import DeploymentPreconditionError
from olf.profile import Provider, StageName, resolve_topology, validate_deployment_profile
from olf.provider_contracts import ProviderContractError, parse_provider_contracts
from olf.tooling.docker import Docker
from olf.tooling.helm import Helm
from olf.tooling.kind import Kind
from olf.tooling.kubectl import Kubectl
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver
from olf.tooling.terraform import Terraform
from olf.tooling.toolkit import Toolkit

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures"

every_provider = pytest.mark.parametrize("provider", tuple(Provider), ids=[p.value for p in Provider])

_FAKE_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm", "aws", "az")

# What a cloud foundation reports once applied. Only the cloud platform
# apply reads it; the local one derives its context statically.
_FOUNDATION_FACTS = FoundationFacts(
    cluster_name="olf-conformance",
    kube_context="olf-conformance",
    project_code_repository="registry.example/project-code",
)

# `DeploymentProvider`'s own members, read off the Protocol rather than
# restated: a member added there has to be implemented by every adapter, and
# this suite is where that becomes true rather than a review comment.
# Read-only members count: most of the Protocol is declared as properties,
# because that is what every adapter actually offers, and annotations alone
# would silently cover only the settable remainder.
_PROTOCOL_ATTRIBUTES = tuple(
    sorted(
        set(DeploymentProvider.__annotations__)
        | {name for name, value in vars(DeploymentProvider).items() if isinstance(value, property)}
    )
)
# The provider-neutral settings surface `DeploymentProvider.config` promises,
# read off `DeploymentConfig` the same way.
_CONFIG_ATTRIBUTES = tuple(
    sorted(name for name, value in vars(DeploymentConfig).items() if isinstance(value, property))
)
_PROTOCOL_METHODS = tuple(
    sorted(
        name
        for name, value in vars(DeploymentProvider).items()
        if inspect.isfunction(value) and not name.startswith("__")
    )
)

# ADR 0002's three phases, in the order `olf deploy` must run them, with the
# adapter step each one dispatches to. PREFETCH is olf's own pre-platform
# image step rather than an ADR phase; it sits here because it is part of the
# order every adapter is driven in (it is deliberately inert on cloud).
_ADR_0002_PHASES = (DeploymentPhase.FOUNDATION, DeploymentPhase.PLATFORM, DeploymentPhase.ARTIFACTS)
_PHASE_STEP = {
    DeploymentPhase.FOUNDATION: "foundation_up",
    DeploymentPhase.PREFETCH: "prepare_images",
    DeploymentPhase.PLATFORM: "platform_up",
    DeploymentPhase.ARTIFACTS: "artifacts_deploy",
}

# Terraform outputs shared, provider-neutral tooling reads by name from every
# platform root. `stage_names` is the load-bearing one: a root that does not
# declare it makes `terraform output` answer "output variable requested could
# not be found", which `applied_stage_names` reads as "nothing applied yet" -
# silently disarming the stage-removal guard on that provider.
_REQUIRED_ROOT_OUTPUTS = ("provider_contracts", "shared_namespace", "stage_names")


_EXTERNAL_IDENTITY = {
    "issuer": "external",
    "issuer_url": "https://login.example.com/",
    "role_claim": "groups",
    "role_mapping": {"platform-admin": ["olf-admins"]},
}

def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


def _fail() -> CommandResult:
    return CommandResult(argv=(), returncode=1, stdout="", stderr="", duration_seconds=0.0)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    resolver = PathExecutableResolver(overrides={name: Path(name) for name in _FAKE_TOOLS})
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
    )


def _permitted_calls(protocol_method: Any) -> tuple[tuple[tuple[Any, ...], dict[str, Any]], ...]:
    """The (args, kwargs) shapes a caller holding only the Protocol may use.

    Optional arguments are both omitted and supplied, and a
    POSITIONAL_OR_KEYWORD parameter is offered both ways: the Protocol permits
    `plan(phase)` and `plan(phase=...)` alike, so an adapter narrowing one to
    positional-only rejects a call its callers may legally make. This is the
    whole space for this Protocol - no *args/**kwargs, no overloads.
    """
    marker = object()
    parameters = list(inspect.signature(protocol_method).parameters.values())[1:]  # drop `self`
    positional: list[Any] = []
    by_name: dict[str, Any] = {}
    required: dict[str, Any] = {}
    optional: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is not parameter.empty:
            optional[parameter.name] = marker
        elif parameter.kind is parameter.KEYWORD_ONLY:
            required[parameter.name] = marker
        else:
            positional.append(marker)
            if parameter.kind is parameter.POSITIONAL_OR_KEYWORD:
                by_name[parameter.name] = marker
    calls = [
        (tuple(positional), dict(required)),
        (tuple(positional), {**required, **optional}),
    ]
    if len(by_name) == len(positional):
        # Every positional slot can also be named, so the all-keyword form is
        # a call the Protocol permits too.
        calls.append(((), {**by_name, **required}))
        calls.append(((), {**by_name, **required, **optional}))
    return tuple(calls)


def _calls_the_adapter_rejects(protocol_method: Any, implementation: Any) -> list[str]:
    """Protocol-shaped calls the bound implementation would not accept.

    Binds rather than compares signature text: an adapter may spell an
    annotation differently, narrow a return type, or add an argument of its own
    with a default, and none of that changes what a shared caller can do. Only
    a call the Protocol permits and the adapter refuses is a contract break.
    """
    rejected = []
    for args, kwargs in _permitted_calls(protocol_method):
        try:
            inspect.signature(implementation).bind(*args, **kwargs)
        except TypeError:
            rendered = ", ".join(["<positional>"] * len(args) + [f"{name}=..." for name in kwargs])
            rejected.append(f"{protocol_method.__name__}({rendered})")
    return rejected


def _context(provider: Provider, repo_root: Path, **kwargs: Any) -> DeploymentContext:
    factory = getattr(DeploymentContext, provider.value, None)
    assert factory is not None, (
        f"{provider.value!r} has no DeploymentContext.{provider.value}() factory. Every provider needs one: "
        "it is how shared code resolves that provider's paths, kube context, and scoped command environment "
        "without knowing which provider it is running against."
    )
    return factory(repo_root=repo_root, **kwargs)


def _adapter(provider: Provider, repo_root: Path, runner: RecordingRunner | None = None, **kwargs: Any):  # noqa: ANN202
    """The provider's adapter, built the way `olf deploy` builds it."""
    context = _context(provider, repo_root, **kwargs)
    return build_provider(context, toolkit=_toolkit(runner or RecordingRunner()), environ={})


def _contract(provider: Provider) -> dict[str, Any]:
    path = FIXTURES / f"{provider.value}-provider-contracts-v3.json"
    assert path.is_file(), (
        f"{provider.value!r} has no captured provider contract at {path.name}. Every provider must publish one: "
        "capture its root's `terraform output -json provider_contracts` so this suite can assert the contract "
        "olf parses at runtime, rather than trusting the adapter."
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    declared = document.get("deployment", {}).get("provider")
    assert declared == provider.value, (
        f"{path.name} declares deployment.provider {declared!r}, not {provider.value!r}. Nothing else ties a "
        f"fixture back to the provider it stands for -- `_topology_of` derives the expected topology from this "
        f"same field, so a copied or mislabelled capture would validate one provider's contract while this "
        f"suite reported it as another's coverage."
    )
    return document


def _topology_of(contract: dict[str, Any]):  # noqa: ANN202 - DeploymentTopology
    """The Deployment Profile that contract claims to serve, resolved."""
    deployment = contract["deployment"]
    return resolve_topology(
        validate_deployment_profile(
            {
                "apiVersion": "openlakeforge.io/v1alpha1",
                "kind": "DeploymentProfile",
                "metadata": {"name": deployment["profile_name"]},
                "spec": {
                    "provider": {
                        "type": deployment["provider"],
                        **({"region": deployment["region"]} if deployment["region"] else {}),
                    },
                    "preset": "slim",
                    **({"access": {}} if contract["shared"]["access"]["implementation"] == "access.ingress" else {}),
                    # Authentication off is local-only, so cloud fixtures carry an identity block.
                    **({"identity": _EXTERNAL_IDENTITY} if deployment["provider"] != "local" else {}),
                    "stages": {
                        name: {
                            "enabled": True,
                            "capabilities": {
                                "analytics": "reporting" in stage,
                                "governance": "governance" in stage,
                            },
                        }
                        for name, stage in contract["stages"].items()
                    },
                },
            }
        )
    )


def _single_stage_topology(provider: Provider, *, enabled: tuple[StageName, ...]):  # noqa: ANN202
    contract = _contract(provider)
    deployment = contract["deployment"]
    return resolve_topology(
        validate_deployment_profile(
            {
                "apiVersion": "openlakeforge.io/v1alpha1",
                "kind": "DeploymentProfile",
                "metadata": {"name": deployment["profile_name"]},
                "spec": {
                    "provider": {
                        "type": deployment["provider"],
                        **({"region": deployment["region"]} if deployment["region"] else {}),
                    },
                    "preset": "slim",
                    **({"access": {}} if contract["shared"]["access"]["implementation"] == "access.ingress" else {}),
                    # Authentication off is local-only, so cloud fixtures carry an identity block.
                    **({"identity": _EXTERNAL_IDENTITY} if deployment["provider"] != "local" else {}),
                    "stages": {stage.value: {"enabled": True} for stage in enabled},
                },
            }
        )
    )


def _root_locals(terraform_root: Path) -> dict[str, Any]:
    """Every `locals { }` block in the root's contract surface, flattened.

    Values built from `merge(...)`/`for` comprehensions collapse to one opaque
    `${...}` expression string: `hcl2` parses HCL syntax, not Terraform
    semantics. Callers that need those scope their assertion to that string.
    """
    document = hcl2.loads((terraform_root / "contracts.tf").read_text(encoding="utf-8"))
    merged: dict[str, Any] = {}
    for block in document.get("locals", []):
        merged.update(block)
    return merged


def _guard_runner(
    *,
    applied_stages: str | None = None,
    legacy_namespace_exists: bool = False,
    labelled_namespaces: str = "",
) -> RecordingRunner:
    """A runner standing in for the cluster a platform apply queries on its way
    to the destructive-apply guards: a reachable kube context, cached charts,
    `terraform output -json stage_names`, the pre-v0.3 shared namespace, and the
    namespaces labelled as this deployment's."""

    class _Runner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]  # noqa: ANN001, ANN202
            argv = list(command.argv) if hasattr(command, "argv") else [str(part) for part in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if "stage_names" in argv:
                return _ok(applied_stages if applied_stages is not None else "[]")
            if argv[0] == "kubectl" and "get-contexts" in argv:
                return _ok("kind-openlakeforge-local\n")
            if "get" in argv and "namespace" in argv and "lakehouse" in argv:
                return _ok() if legacy_namespace_exists else _fail()
            if "namespace" in argv and "-l" in argv:
                return _ok(labelled_namespaces)
            if argv[0] == "kubectl" and "namespace" in argv and "get" in argv:
                return _fail()
            # A cache hit, so a platform apply reaching the guards never pulls
            # or repacks a real chart.
            if argv[0] == "helm" and argv[1:3] == ["show", "chart"]:
                return _ok()
            return _ok()

    return _Runner()


def _ready_for_platform_apply(config: Any) -> None:
    """The preconditions every provider's platform apply checks before it
    reaches the guards: an applied foundation, and a cached chart per release."""
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}", encoding="utf-8")
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    for setting in config.charts.values():
        if setting.package_path is not None:
            Path(setting.package_path).parent.mkdir(parents=True, exist_ok=True)
            Path(setting.package_path).write_text("cached", encoding="utf-8")


def _platform_apply(provider: Provider, adapter: Any) -> None:
    """Run this provider's platform apply, the call path the guards live on.

    Dispatched per provider family rather than through `adapter.platform_up()`
    because the cloud provider resolves live foundation facts first (a real
    Terraform state and a reachable cluster) - `FakeCloudBackend` stands in for
    that, and nothing else about the apply is stubbed. Calling the guards
    directly instead would leave this suite green if a provider's apply stopped
    calling them, which is the regression that matters.
    """
    if provider is Provider.LOCAL:
        from olf.deployment.local import platform as local_platform

        local_platform.platform_up(adapter.config, adapter.tools, env={})
        return
    if provider in (Provider.AWS, Provider.AZURE):
        from olf.deployment.cloud import platform as cloud_platform

        cloud_platform.platform_up(
            adapter.config,
            adapter.tools,
            FakeCloudBackend(scope=provider.value),
            _FOUNDATION_FACTS,
            env={},
        )
        return
    raise AssertionError(
        f"{provider.value!r} has no platform apply wired into this suite. Every provider applies its platform "
        f"through one entry point, and the destructive-apply guards live on it - a provider whose apply this "
        f"suite cannot reach is a provider whose guards are unverified."
    )


@every_provider
def test_adapter_satisfies_the_deployment_provider_protocol(provider: Provider, tmp_path: Path) -> None:
    """`build_provider` returns something that structurally *is* a
    `DeploymentProvider` - the seam architectural rule 1 makes shared code go
    through. Signatures are compared exactly: a step that quietly took an
    extra provider-specific argument would type-check at its own call site and
    break the shared engine."""
    adapter = _adapter(provider, tmp_path)

    for attribute in _PROTOCOL_ATTRIBUTES:
        # Class before instance: a cloud adapter resolves `env` by reading the
        # foundation's Terraform outputs on first touch, so a presence check
        # made on the instance would try to reach a cluster. Plain dataclass
        # fields have no class attribute, which is why the instance is still
        # the fallback.
        assert hasattr(type(adapter), attribute) or hasattr(adapter, attribute), (
            f"{type(adapter).__name__} has no {attribute!r}. DeploymentProvider declares it, so shared code "
            f"reads it on every provider; without it that code has to reach past the Protocol to keep working."
        )
    for method in _PROTOCOL_METHODS:
        implementation = getattr(adapter, method, None)
        assert callable(implementation), (
            f"{type(adapter).__name__} does not implement {method}(). Every DeploymentProvider member is a "
            f"lifecycle step `olf deploy`/`olf destroy` or a Make target delegate calls directly."
        )
        rejected = _calls_the_adapter_rejects(getattr(DeploymentProvider, method), implementation)
        assert not rejected, (
            f"{type(adapter).__name__}.{method}() rejects a call DeploymentProvider permits: {rejected!r}. "
            f"Shared code calls every adapter the same way, so an adapter may add optional arguments but may "
            f"not require its own or drop one the Protocol declares."
        )


@every_provider
def test_adapter_exposes_the_command_environment_shared_callers_use(
    provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`olf.deployment.activation` reads `provider.env` on whichever provider
    it is handed. `DeploymentProvider` declares it, so the presence check
    above already covers the attribute; what this adds is that the value
    resolves, which no Protocol can state.

    The value is resolved, not just the attribute: an adapter whose `env` is
    None, raises, or yields something Helm and Docker cannot take as an
    environment would pass an attribute check and fail at activation. A cloud
    adapter reads the foundation's outputs on first touch, so the resolver
    itself is stubbed -- not the property's cache -- leaving the adapter free
    to store what it resolves however it likes.
    """
    adapter = _adapter(provider, tmp_path)

    assert hasattr(type(adapter), "env"), (
        f"{type(adapter).__name__} has no `env`. Stage activation drives Helm and Docker through the provider's "
        f"own command-environment overlay; without it, activation cannot run against this provider."
    )
    if provider is not Provider.LOCAL:
        from olf.deployment.cloud import foundation

        monkeypatch.setattr(foundation, "require_foundation_facts", lambda *a, **k: _FOUNDATION_FACTS)

    env = adapter.env

    assert isinstance(env, Mapping), (
        f"{type(adapter).__name__}.env is {type(env).__name__}, not a mapping. Shared code hands it straight to "
        f"the toolkit as a subprocess environment."
    )
    assert env and all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()), (
        f"{type(adapter).__name__}.env must be a non-empty mapping of str to str; a subprocess environment "
        f"cannot carry anything else. Got {env!r}."
    )
    assert env.get("KUBE_CONTEXT"), (
        f"{type(adapter).__name__}.env carries no KUBE_CONTEXT. Every adapter resolves the cluster its commands "
        f"target into the command environment - on cloud from the foundation outputs, on local statically."
    )


@every_provider
def test_adapter_config_carries_the_shared_settings_surface(provider: Provider, tmp_path: Path) -> None:
    """Stage activation prepares the Dagster chart and retries
    the apply off `provider.config`, whichever provider it was handed. The
    two concrete configs diverge, so `DeploymentConfig` names the subset
    shared callers may rely on - asserted here per adapter, because a
    Protocol only states the shape and an adapter can still resolve it to
    something activation cannot use.
    """
    config = _adapter(provider, tmp_path).config

    for attribute in _CONFIG_ATTRIBUTES:
        assert hasattr(config, attribute), (
            f"{type(config).__name__} has no {attribute!r}. DeploymentConfig declares it, so provider-neutral "
            f"code reads it on every provider's config; without it that code reaches past the Protocol again."
        )
    assert config.charts["dagster"] is not None, (
        f"{type(config).__name__} resolves no 'dagster' chart. Stage activation installs the user-deployments "
        f"subchart out of that chart's own archive on every provider."
    )
    assert config.terraform.apply_retry.max_attempts >= 1, (
        f"{type(config).__name__}.terraform.apply_retry allows "
        f"{config.terraform.apply_retry.max_attempts} attempts; a policy that never runs the command is not a "
        f"retry policy."
    )

@every_provider
def test_platform_root_declares_a_v3_stage_indexed_contract_surface(provider: Provider) -> None:
    """The root emits one contract keyed by stage, not one contract per apply.

    Deliberately shape-agnostic about the bindings themselves (those are
    `parse_provider_contracts`' job, asserted from the captured output below):
    what is checked here is that the surface is a v3 index built over the
    enabled stages, with every stage-scoped reference pinned to the stage
    being emitted rather than to a fixed one.
    """
    terraform_root = _context(provider, REPO_ROOT).paths.platform_terraform_dir
    assert (terraform_root / "contracts.tf").is_file(), (
        f"{provider.value!r} has no contract surface at {terraform_root}/contracts.tf. Every provider's platform "
        f"root declares one: it is the only thing olf reads a deployment's physical values from."
    )
    contract_surface = _root_locals(terraform_root).get("provider_contracts")

    assert contract_surface is not None, (
        f"{terraform_root}/contracts.tf declares no `provider_contracts` local. It is the single value the root "
        f"publishes to olf; a root without it has no contract at all."
    )
    assert contract_surface.get("schema_version") == "3.0.0", (
        f"{terraform_root}/contracts.tf emits provider-contract schema "
        f"{contract_surface.get('schema_version')!r}. Every root emits v3: v2 is a compatibility adaptation for "
        f"already-applied deployments, not something a root may still be written against."
    )
    stages = str(contract_surface.get("stages", ""))
    assert "local.enabled_stages" in stages, (
        f"{terraform_root}/contracts.tf does not build `provider_contracts.stages` over `local.enabled_stages`. "
        f"The contract is a stage index: every enabled stage gets its own bindings from the resolved topology, "
        f"and no stage may be hardcoded."
    )
    for reference in (
        "stage/${name}/runtime_identity",
        "stage/${name}/catalog",
        "stage/${name}/orchestration",
        "activations/${name}",
        "shared/ops_storage",
    ):
        assert reference in stages, (
            f"{terraform_root}/contracts.tf never emits {reference!r} anywhere in its stage index. A root that "
            f"dropped stage interpolation entirely, or renamed a binding, fails here.\n\n"
            f"This is a presence check, not a per-field one: hcl2 gives `stages` back as one opaque expression "
            f"string, so it cannot tell which field a token came from. Pinning the wrong stage into one field "
            f"while another still carries the token passes this and is caught only by a rendered contract - "
            f"`olf e2e run`, not this suite."
        )


@every_provider
def test_platform_root_declares_the_outputs_shared_tooling_reads(provider: Provider) -> None:
    """Missing `stage_names` does not fail loudly: `terraform output` answers
    "output variable requested could not be found", which `applied_stage_names`
    reads as an unapplied root - so the stage-removal guard would wave through
    the very apply it exists to stop, on that provider only."""
    terraform_root = _context(provider, REPO_ROOT).paths.platform_terraform_dir
    document = hcl2.loads((terraform_root / "outputs.tf").read_text(encoding="utf-8"))
    declared = {name for block in document.get("output", []) for name in block}

    for output in _REQUIRED_ROOT_OUTPUTS:
        assert output in declared, (
            f"{terraform_root}/outputs.tf does not declare the {output!r} output. Provider-neutral olf code reads "
            f"it by name on every provider, and a missing Terraform output reads as an empty one rather than as "
            f"an error."
        )


@every_provider
def test_every_enabled_stage_resolves_from_the_contract(provider: Provider) -> None:
    """The contract the root emits parses, and serves every enabled stage."""
    contract = _contract(provider)
    topology = _topology_of(contract)

    parsed = parse_provider_contracts(contract, topology)

    enabled = {stage.name for stage in topology.stages if stage.enabled}
    assert set(parsed.stages) == enabled, (
        f"{provider.value}'s contract serves {sorted(stage.value for stage in parsed.stages)!r} but its topology "
        f"enables {sorted(stage.value for stage in enabled)!r}. A stage the profile enables and the contract "
        f"cannot serve is a stage whose deploy fails after the platform is already applied."
    )
    for stage in enabled:
        assert parsed.for_stage(stage).name == stage, (
            f"{provider.value}'s contract does not resolve the {stage.value!r} stage by name."
        )
    absent = next((stage for stage in StageName if stage not in enabled), None)
    if absent is not None:
        with pytest.raises(ProviderContractError, match=absent.value):
            parsed.for_stage(absent)


@every_provider
def test_logical_stage_identities_derive_from_stage_identity_alone(provider: Provider) -> None:
    """AGENTS.md architectural rule 2, on the names the rule actually governs.

    Namespace, SQL catalog name, and activation prefix are computed by shared
    provider-neutral code from the stage name, so each has exactly one correct
    value on every provider. Physical object-store and catalog naming is
    explicitly delegated to the provider contract and is deliberately not
    pinned here - see the shape assertions below, and
    `test_logical_stage_identities_are_identical_across_providers`.
    """
    contract = _contract(provider)
    parsed = parse_provider_contracts(contract, _topology_of(contract))

    for name, stage in parsed.stages.items():
        for field, actual, canonical in _logical_identities(name, stage):
            assert actual == canonical, (
                f"{provider.value}'s {name.value!r} stage names its {field} {actual!r}, not the canonical "
                f"{canonical!r}. Shared code derives this name from the stage alone, so a provider that spells "
                f"it differently is one shared code cannot address."
            )


@every_provider
def test_physical_stage_storage_stays_isolated_per_stage(provider: Provider) -> None:
    """The provider contract owns physical bucket naming (rule 2 delegates it),
    so this asserts the property stage isolation depends on rather than the
    spelling: each layer's bucket is distinct, and belongs to exactly one stage
    and one layer."""
    contract = _contract(provider)
    parsed = parse_provider_contracts(contract, _topology_of(contract))

    seen: dict[str, str] = {}
    for name, stage in parsed.stages.items():
        for layer in _MEDALLION_LAYERS:
            bucket = str(stage.storage[layer]["bucket_name"])
            owner = f"{name.value}/{layer}"
            assert bucket not in seen, (
                f"{provider.value} gives {owner} the same bucket as {seen[bucket]}: {bucket!r}. Stage data-plane "
                f"isolation is what keeps DEV out of PROD's data; two stages sharing a bucket removes it."
            )
            seen[bucket] = owner


@every_provider
def test_contract_environment_resolves_only_the_stage_it_was_built_for(provider: Provider) -> None:
    """#134, at the layer `olf` itself owns: the environment `build_contract_env`
    resolves for one stage names that stage's bindings and no other's. Every
    `olf` command and the activation rollout configure themselves from it.

    Scope, stated because the criterion is broader than this test: the Dagster
    container's own environment is rendered independently inside
    `infra/terraform/modules/orchestration/dagster/main.tf` from the contracts
    the root passes it, and nothing here evaluates that. `olf check contracts`
    guards the root wiring as expression text; asserting the rendered values
    needs an applied plan and is not covered anywhere yet.

    Three passes, because no one of them holds on its own.

    Equality pins each contracted binding in `_STAGE_BINDINGS` to the selected
    stage's own value. Absence alone would not: an environment handing all
    three stages the generic `lakehouse-bronze`/`-silver`/`-gold` defaults
    names no other stage while DEV and PROD share every bucket.

    The stage-word scan then covers everything built out of those bindings --
    catalog FQNs, the schema JSON blobs, the SQLAlchemy URI -- which equality
    cannot check without restating their derivation.

    Classification completeness is the third: any export that varies between
    stages and is in none of the three lists fails here. Without it, a
    stage-carrying variable added later is simply unasserted, which is how
    `OPENLAKEFORGE_DBT_TRINO_USER` -- Trino's authentication boundary between
    stages -- went uncovered until a review caught it.
    """
    contract = _contract(provider)
    topology = _topology_of(contract)
    parsed = parse_provider_contracts(contract, topology)
    per_stage = {
        name: build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage=name)[0]
        for name in parsed.stages
    }

    for name, stage in parsed.stages.items():
        exports = per_stage[name]
        wrong = {
            key: (exports.get(key), str(expected(stage)))
            for key, expected in _STAGE_BINDINGS
            if key in exports and exports[key] != str(expected(stage))
        }
        assert not wrong, (
            f"{provider.value}'s {name.value!r} environment binds {wrong!r} as (emitted, contracted). Every one "
            f"of these is how a run reaches data or authenticates to Trino, so a binding that is not this "
            f"stage's own is one stage reading or writing another's -- including the case where every stage is "
            f"handed the same generic default."
        )
        prefix = str(stage.activation["prefix"])
        astray = {
            key: exports[key]
            for key, suffix in _ACTIVATION_URIS.items()
            if key in exports and not exports[key].endswith(f"/{prefix}{suffix}")
        }
        assert not astray, (
            f"{provider.value}'s {name.value!r} activation artifacts land at {astray!r}, outside its contracted "
            f"{prefix!r} prefix. Stages share the ops bucket; the prefix is the only thing keeping one stage's "
            f"manifests, logs and run artifacts out of another's."
        )

        others = {other.value for other in parsed.stages if other != name}
        leaked = sorted(
            (key, other)
            for key, value in exports.items()
            for other in others
            if _names_stage(value, other)
        )
        assert not leaked, (
            f"{provider.value}'s {name.value!r} environment carries {leaked!r} as (export, stage named). A "
            f"command or rollout handed another stage's catalog, bucket or principal reads, writes or "
            f"authenticates as that stage."
        )

    varying = {
        key
        for key in set().union(*(set(exports) for exports in per_stage.values()))
        if len({exports.get(key) for exports in per_stage.values()}) > 1
    }
    unclassified = sorted(
        varying
        - {key for key, _ in _STAGE_BINDINGS}
        - set(_ACTIVATION_URIS)
        - _STAGE_DERIVED_EXPORTS
        - _CAPABILITY_GATED_EXPORTS
    )
    assert not unclassified, (
        f"{provider.value} emits {unclassified!r} with a different value per stage, and nothing in this module "
        f"classifies them. Add each to _STAGE_BINDINGS with the contracted binding it must equal, to "
        f"_STAGE_DERIVED_EXPORTS if it is built from one, or to _CAPABILITY_GATED_EXPORTS if it varies because "
        f"a stage turned a capability off. Leaving it here means a stage-carrying value nobody asserts."
    )


def _stage_pod_env(contract: dict[str, Any], topology: Any, stage: Any) -> dict[str, str]:
    """The env a stage's code location runs with: its contract exports through activation's filter."""
    from olf.deployment.activation import _user_values
    from olf.project_activation import ProjectActivation

    exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage=stage.name)
    activation = ProjectActivation(
        deployment_profile=contract["deployment"]["profile_name"],
        provider=contract["deployment"]["provider"],
        stage=stage.name,
        project_name="demo",
        project_revision="sha256:" + "b" * 64,
        distribution_version="0.0.0",
        project_code_image="ghcr.io/acme/project-code@sha256:" + "a" * 64,
        provider_binding_digest="sha256:" + "d" * 64,
        capabilities={"analytics": stage.reporting is not None, "governance": stage.governance is not None},
    ).resolved()
    values = _user_values(
        activation,
        contract_environ=exports,
        namespace=stage.namespace,
        platform_globals={},
        code_locations=stage.code_locations,
    )
    return {entry["name"]: entry["value"] for entry in values["deployments"][0]["env"] if "value" in entry}


@pytest.mark.parametrize("provider", [Provider.LOCAL, Provider.AZURE])
def test_each_stage_renders_a_floe_profile_bound_to_its_own_contract(provider: Provider) -> None:
    """The last hop #546 asks to prove: contract, then activation's pod env, then the Floe profile.

    The export-level test above stops at the contract environment. This one
    renders the profile from what activation actually puts on the pod, so a
    Floe input the runtime allowlist drops, or one stage's value reaching
    another stage's profile, fails here rather than in a run.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from libs.floe_profile import render_profile

    contract = _contract(provider)
    topology = _topology_of(contract)
    parsed = parse_provider_contracts(contract, topology)
    assert len(parsed.stages) > 1

    for name, stage in parsed.stages.items():
        profile = render_profile(_stage_pod_env(contract, topology, stage))

        buckets = {storage["name"]: storage["bucket"] for storage in profile["storages"]["definitions"]}
        assert buckets["lakehouse_bronze"] == stage.storage["bronze"]["bucket_name"]
        assert buckets["lakehouse_silver"] == stage.storage["silver"]["bucket_name"]
        (catalog,) = profile["catalogs"]["definitions"]
        assert catalog["warehouse"] == stage.catalog.get("warehouse", stage.catalog["catalog_name"])
        runner = profile["execution"]["runner"]
        assert runner["namespace"] == stage.namespace
        assert profile["metadata"]["name"] == name.value
        others = {other.value for other in parsed.stages if other != name}
        rendered = json.dumps(profile)
        assert not [other for other in others if _names_stage(rendered, other)], (
            f"{provider.value}'s {name.value!r} Floe profile names another stage: {rendered}"
        )


def test_each_aws_stage_writes_silver_into_its_own_glue_databases() -> None:
    """The account's one Glue catalog is shared by every stage, so each stage's
    profile must name its own prefixed database per domain -- the one
    `olf catalog sync-namespaces` creates -- from what activation puts on the pod."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from libs.floe_profile import render_profile

    contract = _contract(Provider.AWS)
    topology = _topology_of(contract)
    parsed = parse_provider_contracts(contract, topology)

    for name, stage in parsed.stages.items():
        profile = render_profile(_stage_pod_env(contract, topology, stage))

        (catalog,) = profile["catalogs"]["definitions"]
        assert catalog["type"] == "glue"
        assert catalog["database"] == f"{stage.catalog['catalog_name']}_{{domain}}"
        assert catalog["create_database_if_missing"] is False
        buckets = {storage["name"]: storage["bucket"] for storage in profile["storages"]["definitions"]}
        assert buckets["lakehouse_silver"] == stage.storage["silver"]["bucket_name"]
        runner = profile["execution"]["runner"]
        assert runner["namespace"] == stage.namespace
        assert not [secret for secret in runner["secrets"] if secret["name"].startswith("POLARIS_")]
        others = {other.value for other in parsed.stages if other != name}
        rendered = json.dumps(profile)
        assert not [other for other in others if _names_stage(rendered, other)], (
            f"aws's {name.value!r} Floe profile names another stage: {rendered}"
        )


def test_a_stage_bucket_that_extends_another_stages_name_is_not_a_leak() -> None:
    """Physical bucket naming is the provider's to choose (AGENTS.md rule 2), so
    a deployment whose DEV bucket is a prefix of its PROD bucket is legal and
    its two bindings are distinct. Scanning for one whole name inside the other
    reports PROD's own bucket as carrying DEV's -- a false positive that fails
    a correct provider. Scanning for the stage *word* is what avoids it, since
    `acme-data-bronze-prod` contains no word `dev`.
    """
    contract = copy.deepcopy(_contract(Provider.LOCAL))
    for stage_name, suffix in (("dev", ""), ("prod", "-prod")):
        for layer in _MEDALLION_LAYERS:
            bucket = f"acme-data-{layer}{suffix}"
            contract["stages"][stage_name]["storage"][layer] = {
                "physical_id": bucket,
                "bucket_name": bucket,
                "uri": f"s3://{bucket}",
            }
    topology = _topology_of(contract)
    parsed = parse_provider_contracts(contract, topology)

    for name, stage in parsed.stages.items():
        exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage=name)
        assert exports["OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"] == str(stage.storage["bronze"]["bucket_name"])

    prod_exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage=StageName.PROD)
    leaked = sorted(key for key, value in prod_exports.items() if _names_stage(value, StageName.DEV.value))
    assert not leaked, (
        f"PROD's own 'acme-data-<layer>-prod' buckets were reported as naming the DEV stage in {leaked!r}."
    )


def test_logical_stage_identities_are_identical_across_providers() -> None:
    """Each provider gives the conformance stages the same logical identities.

    Physical bucket names and principals remain provider-owned. Using the
    declared baseline instead of intersecting fixtures makes a missing stage
    fail rather than disappear from the comparison.
    """
    parsed = {}
    for provider in Provider:
        contract = _contract(provider)
        parsed[provider] = parse_provider_contracts(contract, _topology_of(contract))
    profiles = {provider.value: contracts.deployment["profile_name"] for provider, contracts in parsed.items()}
    assert len(set(profiles.values())) == 1, (
        f"the captured contracts describe different Deployment Profiles ({profiles!r}), so nothing here compares "
        f"like with like. Capture every provider's contract from the same profile."
    )
    for provider, contracts in parsed.items():
        missing = sorted(stage.value for stage in CONFORMANCE_STAGES if stage not in contracts.stages)
        assert not missing, (
            f"{provider.value}'s captured contract does not serve {missing!r}. Every provider must serve the "
            f"same baseline stages for this suite to compare them; a provider missing one is the omission the "
            f"suite exists to catch, not a case to skip."
        )

    for stage in CONFORMANCE_STAGES:
        identities = {
            provider.value: tuple(
                actual for _, actual, _ in _logical_identities(stage, contracts.for_stage(stage))
            )
            for provider, contracts in parsed.items()
        }
        assert len(set(identities.values())) == 1, (
            f"the {stage.value!r} stage is addressed by different logical names per provider: {identities!r}. "
            f"These are derived from the stage alone, so a difference here means shared code has to know which "
            f"provider it is talking to."
        )


def _apply_ready_adapter(provider: Provider, tmp_path: Path, runner: RecordingRunner, **kwargs: Any):  # noqa: ANN202
    """An adapter whose platform apply can run as far as the guards."""
    adapter = _adapter(
        provider,
        tmp_path,
        runner=runner,
        topology=_single_stage_topology(provider, enabled=(StageName.DEV,)),
        **kwargs,
    )
    _ready_for_platform_apply(adapter.config)
    return adapter


@every_provider
def test_a_platform_apply_refuses_to_remove_an_applied_stage(provider: Provider, tmp_path: Path) -> None:
    """Driven through the provider's platform apply, not by calling the guard:
    a provider whose apply stops calling `require_no_stage_removal` has to fail
    here. What it protects is an apply that is already deleting a namespace,
    its services, and their credentials."""
    adapter = _apply_ready_adapter(provider, tmp_path, _guard_runner(applied_stages='["dev", "prod"]'))

    with pytest.raises(DeploymentPreconditionError, match="prod"):
        _platform_apply(provider, adapter)


@every_provider
def test_a_platform_apply_refuses_a_stage_dropped_only_from_the_cluster(provider: Provider, tmp_path: Path) -> None:
    """The drift path: Terraform state is gone, so `stage_names` reports
    nothing applied, and the namespaces still labelled as this deployment's are
    the only signal left that a dropped stage is deployed."""
    adapter = _apply_ready_adapter(
        provider, tmp_path, _guard_runner(labelled_namespaces="olf-system\nolf-dev\nolf-prod\n")
    )

    with pytest.raises(DeploymentPreconditionError, match="prod"):
        _platform_apply(provider, adapter)


@every_provider
def test_a_platform_apply_proceeds_past_removal_under_the_explicit_opt_in(
    provider: Provider, tmp_path: Path
) -> None:
    """`--allow-stage-removal` is the one thing that lets the apply through,
    and it means the same on every provider."""
    adapter = _apply_ready_adapter(
        provider, tmp_path, _guard_runner(applied_stages='["dev", "prod"]'), allow_stage_removal=True
    )

    _platform_apply(provider, adapter)


@every_provider
def test_a_platform_apply_refuses_to_replace_the_pre_v0_3_shared_namespace(
    provider: Provider, tmp_path: Path
) -> None:
    """Every root's shared-services namespace was `lakehouse` before the
    stage-aware rewrite. A namespace name is immutable, so an apply against an
    unmigrated cluster plans to destroy it - and the SeaweedFS, PostgreSQL, and
    Polaris state inside it - to create an empty `olf-system`."""
    adapter = _apply_ready_adapter(provider, tmp_path, _guard_runner(legacy_namespace_exists=True))

    with pytest.raises(DeploymentPreconditionError, match="lakehouse"):
        _platform_apply(provider, adapter)


@every_provider
def test_a_platform_apply_proceeds_on_a_fresh_cluster(provider: Provider, tmp_path: Path) -> None:
    """Neither guard fires where there is nothing deployed to destroy."""
    adapter = _apply_ready_adapter(provider, tmp_path, _guard_runner())

    _platform_apply(provider, adapter)


def _record_steps(adapter_class: type, monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Replace each ADR 0002 step with a recorder, asserting it exists first.

    Recording is what keeps these two tests cluster-free, and is also their
    limit: they check which step each phase selects and in what order, never
    what a step does.
    """
    for step in _PHASE_STEP.values():
        assert hasattr(adapter_class, step), (
            f"{adapter_class.__name__} does not implement {step}(). `olf deploy --phase` selects it by name on "
            f"every provider; a provider with nothing to do there implements it as a no-op rather than omitting it."
        )

        def _recorder(self, *args: Any, step: str = step, **kwargs: Any) -> None:  # noqa: ANN001, ARG001
            calls.append(step)

        monkeypatch.setattr(adapter_class, step, _recorder)


@every_provider
def test_every_deploy_phase_selects_its_own_adapter_step(
    provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0002: each phase is selectable with `--phase` on every provider, and
    selecting one dispatches that adapter's step alone.

    Scope: the steps are recorded, so this proves the phase-to-step wiring and
    that this adapter implements every step by name -- not that a step does
    the right work. An adapter whose `platform_up` became a no-op, or started
    doing artifact work, still passes here; only a real apply shows that, and
    that is `olf e2e run`."""
    write_two_product_fixture(tmp_path, dashboards=(("widgets_overview", "widgets_alpha"),))
    adapter = _adapter(provider, tmp_path)
    calls: list[str] = []
    _record_steps(type(adapter), monkeypatch, calls)
    engine = DeploymentEngine(adapter)

    for phase, step in _PHASE_STEP.items():
        calls.clear()
        engine.deploy(phase)

        assert calls == [step], (
            f"`olf deploy --provider {provider.value} --phase {phase.value}` ran {calls!r} rather than {[step]!r}. "
            f"Each phase runs its own step and nothing else."
        )


@every_provider
def test_deploying_everything_runs_the_adr_0002_order(
    provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foundation -> platform -> artifacts, identically on every provider.
    Static infrastructure is applied before anything derived from
    `lakehouse_code/` exists, which is what keeps a code commit off Terraform.

    Ordering only, for the reason the phase-selection test above gives: the
    steps are recorded rather than run."""
    write_two_product_fixture(tmp_path, dashboards=(("widgets_overview", "widgets_alpha"),))
    adapter = _adapter(provider, tmp_path)
    calls: list[str] = []
    _record_steps(type(adapter), monkeypatch, calls)

    DeploymentEngine(adapter).deploy()

    ordered = [call for call in calls if call in {_PHASE_STEP[phase] for phase in _ADR_0002_PHASES}]
    assert ordered == [_PHASE_STEP[phase] for phase in _ADR_0002_PHASES], (
        f"`olf deploy --provider {provider.value}` ran {calls!r}. ADR 0002 fixes the order as "
        f"foundation -> platform -> artifacts: an artifact step that runs before the platform is applied has no "
        f"platform to deploy onto, and a platform apply that waits on one has made a code commit invoke Terraform."
    )
