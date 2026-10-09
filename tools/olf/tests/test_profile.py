from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from olf.deployment.context import Provider
from olf.profile import (
    AccessSpec,
    DeploymentProfileError,
    Preset,
    StageName,
    legacy_single_stage_topology,
    load_deployment_profile,
    resolve_topology,
    validate_deployment_profile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


def _load_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURES / name).read_text())


def test_load_deployment_profile_parses_the_repo_root_profile() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    profile = load_deployment_profile(repo_root / "openlakeforge.yaml")

    assert profile.name == "openlakeforge"
    assert profile.provider.type == Provider.LOCAL
    assert profile.preset == Preset.SLIM
    assert len(profile.stages) == 1
    assert profile.stage(StageName.DEV) is not None
    assert profile.stage(StageName.DEV).enabled is True


def test_valid_slim_local_profile_resolves_dev_only_with_no_capabilities() -> None:
    profile = validate_deployment_profile(_load_fixture("valid_slim_local.yaml"))
    topology = resolve_topology(profile)

    assert topology.provider == Provider.LOCAL
    assert topology.region is None
    assert topology.preset == Preset.SLIM

    dev = topology.stage(StageName.DEV)
    assert dev.enabled is True
    assert dev.capabilities.analytics is False
    assert dev.capabilities.governance is False

    for stage_name in (StageName.UAT, StageName.PROD):
        stage = topology.stage(stage_name)
        assert stage.enabled is False
        assert stage.capabilities.analytics is False
        assert stage.capabilities.governance is False


def test_valid_full_aws_profile_applies_preset_defaults_and_explicit_overrides() -> None:
    profile = validate_deployment_profile(_load_fixture("valid_full_aws_with_uat_prod.yaml"))
    topology = resolve_topology(profile)

    assert topology.provider == Provider.AWS
    assert topology.region == "eu-west-3"
    assert topology.preset == Preset.FULL

    # dev/uat wrote no capabilities -> the 'full' preset default applies.
    for stage_name in (StageName.DEV, StageName.UAT):
        stage = topology.stage(stage_name)
        assert stage.enabled is True
        assert stage.capabilities.analytics is True
        assert stage.capabilities.governance is True

    # prod explicitly overrides governance -> explicit wins over the preset.
    prod = topology.stage(StageName.PROD)
    assert prod.enabled is True
    assert prod.capabilities.analytics is True
    assert prod.capabilities.governance is False


def test_disabled_stage_forces_capabilities_false_even_if_written_true() -> None:
    document = _load_fixture("valid_full_aws_with_uat_prod.yaml")
    document["spec"]["stages"]["uat"] = {"enabled": False, "capabilities": {"analytics": True, "governance": True}}
    profile = validate_deployment_profile(document)

    topology = resolve_topology(profile)

    uat = topology.stage(StageName.UAT)
    assert uat.enabled is False
    assert uat.capabilities.analytics is False
    assert uat.capabilities.governance is False


def test_missing_stage_resolves_disabled_and_prod_is_never_implicit() -> None:
    profile = validate_deployment_profile(_load_fixture("valid_slim_local.yaml"))

    topology = resolve_topology(profile)

    assert topology.stage(StageName.PROD).enabled is False


def test_render_json_is_stable_across_repeated_resolutions() -> None:
    profile = validate_deployment_profile(_load_fixture("valid_full_aws_with_uat_prod.yaml"))

    first = resolve_topology(profile).render_json()
    second = resolve_topology(profile).render_json()

    assert first == second


@pytest.mark.parametrize(
    ("fixture_name", "match"),
    [
        ("invalid_unsupported_api_version.yaml", "unsupported apiVersion"),
        ("invalid_unknown_envelope_field.yaml", "must not contain"),
        ("invalid_unknown_stage.yaml", "unknown stage"),
        ("invalid_no_stage_enabled.yaml", "at least one stage must be enabled"),
        ("invalid_prod_without_dev.yaml", "cannot be enabled while 'dev' is disabled"),
        ("invalid_local_with_region.yaml", "region must not be set"),
        ("invalid_unknown_provider_type.yaml", "spec.provider.type must be one of"),
        ("invalid_unknown_capability_field.yaml", "must not contain"),
    ],
)
def test_validate_deployment_profile_rejects_every_illegal_shape(fixture_name: str, match: str) -> None:
    document = _load_fixture(fixture_name)

    with pytest.raises(DeploymentProfileError, match=match):
        validate_deployment_profile(document)


def test_validate_deployment_profile_rejects_wrong_kind() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["kind"] = "NotAProfile"

    with pytest.raises(DeploymentProfileError, match="kind must be"):
        validate_deployment_profile(document)


@pytest.mark.parametrize("field", ["bucket", "catalog", "endpoint", "credentials"])
def test_validate_deployment_profile_rejects_provider_physical_details(field: str) -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["provider"][field] = "provider-owned"

    with pytest.raises(DeploymentProfileError, match="must not contain"):
        validate_deployment_profile(document)


def test_validate_deployment_profile_rejects_non_mapping_document() -> None:
    with pytest.raises(DeploymentProfileError, match="must contain a YAML object"):
        validate_deployment_profile([])  # type: ignore[arg-type]


def test_load_deployment_profile_reports_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        load_deployment_profile("/nonexistent/openlakeforge.yaml")


def test_legacy_single_stage_topology_matches_the_v02_slim_shorthand() -> None:
    topology = legacy_single_stage_topology(provider=Provider.LOCAL, preset=Preset.SLIM)

    assert topology.provider == Provider.LOCAL
    dev = topology.stage(StageName.DEV)
    assert dev.enabled is True
    assert dev.capabilities.analytics is False
    assert dev.capabilities.governance is False
    for stage_name in (StageName.UAT, StageName.PROD):
        assert topology.stage(stage_name).enabled is False


def test_legacy_single_stage_topology_matches_the_v02_full_shorthand() -> None:
    topology = legacy_single_stage_topology(provider=Provider.LOCAL, preset=Preset.FULL)

    dev = topology.stage(StageName.DEV)
    assert dev.capabilities.analytics is True
    assert dev.capabilities.governance is True


@pytest.mark.parametrize(
    "name",
    ["acme-", "acme_data", "-acme", "1acme", "a" * 64],
)
def test_validate_deployment_profile_rejects_names_kubernetes_cannot_label_with(name: str) -> None:
    """The profile name becomes the `openlakeforge.io/profile` label value on
    every namespace the deployment owns, and teardown selects on it. A name a
    label value cannot hold would validate here and fail the apply that
    creates the namespaces."""
    document = _load_fixture("valid_slim_local.yaml")
    document["metadata"]["name"] = name

    with pytest.raises(DeploymentProfileError, match="metadata.name must match"):
        validate_deployment_profile(document)


@pytest.mark.parametrize("name", ["a", "acme-data", "acme2", "a" + "b" * 62])
def test_validate_deployment_profile_accepts_label_safe_names(name: str) -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["metadata"]["name"] = name

    assert validate_deployment_profile(document).name == name


def test_access_defaults_to_the_local_evaluation_install_and_accepts_a_real_domain() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    assert resolve_topology(validate_deployment_profile(document)).access == AccessSpec("olf.localhost", "local-ca")

    document["spec"]["access"] = {"base_domain": "olf.example.com", "issuer": "letsencrypt"}
    assert resolve_topology(validate_deployment_profile(document)).access == AccessSpec(
        "olf.example.com", "letsencrypt"
    )


@pytest.mark.parametrize(
    ("access", "match"),
    [
        ({"base_domain": "localhost"}, "base_domain must be"),
        ({"base_domain": "OLF.example.com"}, "base_domain must be"),
        ({"issuer": "Let's Encrypt"}, "issuer must match"),
        ({"tls": "none"}, "must not contain"),
    ],
)
def test_access_rejects_names_a_route_host_or_issuer_cannot_use(access: dict, match: str) -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["access"] = access

    with pytest.raises(DeploymentProfileError, match=match):
        validate_deployment_profile(document)


def test_identity_defaults_to_keycloak_and_accepts_an_external_issuer() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    assert validate_deployment_profile(document).identity.issuer == "keycloak"

    document["spec"]["identity"] = {
        "issuer": "external",
        "issuer_url": "https://login.example.com/",
        "role_claim": "cognito:groups",
        "role_mapping": {"platform-admin": ["olf-admins"]},
    }
    assert validate_deployment_profile(document).identity.role_mapping == {"platform-admin": ("olf-admins",)}


@pytest.mark.parametrize(
    ("identity", "match"),
    [
        ({"issuer": "okta"}, "issuer must be one of"),
        ({"issuer": "external"}, "requires"),
        ({"issuer_url": "http://x.example.com"}, "https URL"),
        ({"role_mapping": {"guest": ["g"]}}, "unknown role 'guest'"),
        ({"role_mapping": {"viewer": []}}, "non-empty list"),
        ({"role_mapping": {"viewer": ["g", "g"]}}, "must not repeat"),
        ({"issuer_url": "https://"}, "absolute"),
        ({"issuer_url": "https://idp.example/t?realm=x"}, "no query or fragment"),
        ({"issuer_url": "https://idp.example/t#f"}, "no query or fragment"),
        (
            {"issuer": "external", "issuer_url": None, "role_claim": "g", "role_mapping": {"viewer": ["v"]}},
            "issuer_url",
        ),
        (
            {
                "issuer": "external",
                "issuer_url": "https://i.example",
                "role_claim": None,
                "role_mapping": {"viewer": ["v"]},
            },
            "role_claim",
        ),
        ({"issuer_url": "https://u:p@example.com"}, "credentials"),
        ({"client_secret": "x"}, "must not contain"),
    ],
)
def test_identity_fails_closed(identity: dict, match: str) -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["identity"] = identity

    with pytest.raises(DeploymentProfileError, match=match):
        validate_deployment_profile(document)


def test_identity_survives_topology_resolution() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["identity"] = {
        "issuer": "external",
        "issuer_url": "https://login.example.com/",
        "role_claim": "groups",
        "role_mapping": {"viewer": ["v"]},
    }
    profile = validate_deployment_profile(document)
    assert resolve_topology(profile).identity == profile.identity


def test_identity_client_ids_are_validated() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    base = {
        "issuer": "external",
        "issuer_url": "https://login.example.com/",
        "role_claim": "g",
        "role_mapping": {"viewer": ["v"]},
    }
    document["spec"]["identity"] = base | {"client_ids": {"trino": "0oa1"}}
    assert validate_deployment_profile(document).identity.client_ids == {"trino": "0oa1"}
    for bad in ({"nope": "x"}, {"trino": ""}, {}):
        document["spec"]["identity"] = base | {"client_ids": bad}
        with pytest.raises(DeploymentProfileError):
            validate_deployment_profile(document)


def test_render_json_carries_identity() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["identity"] = {
        "issuer": "external",
        "issuer_url": "https://login.example.com/",
        "role_claim": "groups",
        "role_mapping": {"viewer": ["v"]},
    }
    rendered = json.loads(resolve_topology(validate_deployment_profile(document)).render_json())
    assert rendered["identity"]["issuer_url"] == "https://login.example.com/"
    assert rendered["identity"]["role_mapping"] == {"viewer": ["v"]}


def test_external_identity_is_immutable_and_rejects_a_malformed_authority() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["identity"] = {
        "issuer": "external",
        "issuer_url": "https://login.example.com/",
        "role_claim": "groups",
        "role_mapping": {"viewer": ["v"]},
    }
    profile = validate_deployment_profile(document)
    with pytest.raises(TypeError):
        profile.identity.role_mapping["admin"] = ("x",)  # type: ignore[index]
    assert profile.identity.role_mapping == {"viewer": ("v",)}

    document["spec"]["identity"]["issuer_url"] = "https://[bad]/"
    with pytest.raises(DeploymentProfileError):
        validate_deployment_profile(document)
