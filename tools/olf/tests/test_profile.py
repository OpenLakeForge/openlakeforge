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


def test_no_access_or_identity_block_means_a_quickstart_with_neither() -> None:
    topology = resolve_topology(validate_deployment_profile(_load_fixture("valid_slim_local.yaml")))
    assert topology.access is None and topology.identity is None
    rendered = json.loads(topology.render_json())
    assert rendered["access"] is None and rendered["identity"] is None


def _github_identity() -> dict:
    return _load_fixture("valid_github_identity.yaml")["spec"]["identity"]


def test_identity_without_access_is_rejected() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["identity"] = _github_identity()
    with pytest.raises(DeploymentProfileError, match="spec.identity requires spec.access"):
        validate_deployment_profile(document)


def test_a_cloud_profile_may_declare_identity_without_access() -> None:
    document = _load_fixture("valid_full_aws_with_uat_prod.yaml")
    document["spec"]["identity"] = _github_identity()
    assert validate_deployment_profile(document).access is None


def test_an_empty_access_block_asks_for_the_defaults_and_identity_may_follow() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["access"] = {}
    assert resolve_topology(validate_deployment_profile(document)).access == AccessSpec("olf.localhost", "local-ca")
    document["spec"]["identity"] = _github_identity()
    assert resolve_topology(validate_deployment_profile(document)).identity.issuer == "github"


def test_access_accepts_a_real_domain() -> None:
    document = _load_fixture("valid_slim_local.yaml")

    document["spec"]["access"] = {"base_domain": "olf.example.com", "issuer": "letsencrypt"}
    document["spec"]["identity"] = {}
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


@pytest.mark.parametrize("identity", [{}, {"issuer": "keycloak"}])
def test_identity_needs_an_issuer_of_github_or_external(identity: dict) -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"] |= {"access": {}, "identity": identity}
    with pytest.raises(DeploymentProfileError, match="issuer must be one of"):
        validate_deployment_profile(document)


def test_identity_accepts_an_external_issuer() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"] |= {"access": {}}

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
        ({"issuer_url": "http://x.example.com"}, "https URL"),
        ({"role_mapping": {"guest": ["g"]}}, "unknown role 'guest'"),
        ({"role_mapping": {"viewer": []}}, "non-empty list"),
        ({"role_mapping": {"viewer": ["g", "g"]}}, "must not repeat"),
        ({"role_mapping": {"viewer": ["g"], "platform-admin": ["g"]}}, "more than one role"),
        ({"issuer_url": "https://idp.example/[realm]"}, "outside the host"),
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
    document["spec"].setdefault("access", {})
    external = {
        "issuer": "external",
        "issuer_url": "https://login.example.com/",
        "role_claim": "groups",
        "role_mapping": {"viewer": ["v"]},
    }
    document["spec"]["identity"] = external | identity

    with pytest.raises(DeploymentProfileError, match=match):
        validate_deployment_profile(document)


def test_external_issuer_requires_its_claim_mapping() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"] |= {"access": {}, "identity": {"issuer": "external"}}
    with pytest.raises(DeploymentProfileError, match="requires"):
        validate_deployment_profile(document)


def test_identity_survives_topology_resolution() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"].setdefault("access", {})
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
    document["spec"].setdefault("access", {})
    document["spec"]["identity"] = base | {"client_ids": {"trino": "0oa1"}}
    assert validate_deployment_profile(document).identity.client_ids == {"trino": "0oa1"}
    for bad in ({"nope": "x"}, {"trino": ""}, {}):
        document["spec"].setdefault("access", {})
        document["spec"]["identity"] = base | {"client_ids": bad}
        with pytest.raises(DeploymentProfileError):
            validate_deployment_profile(document)


def test_identity_client_ids_require_external_issuer() -> None:
    with pytest.raises(DeploymentProfileError, match="client_ids requires issuer 'external'"):
        validate_deployment_profile(_github_profile(client_ids={"trino": "0oa1"}))


def test_render_json_carries_identity() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"].setdefault("access", {})
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
    document["spec"].setdefault("access", {})
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


def test_external_identity_roles_come_from_the_given_distribution_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "release").mkdir(parents=True)
    (dist / "release/identity-roles.yaml").write_text("precedence: [viewer]\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv("OLF_DISTRIBUTION_ROOT", raising=False)
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"].setdefault("access", {})
    document["spec"]["identity"] = {
        "issuer": "external",
        "issuer_url": "https://[::1]:8443/realms/x",
        "role_claim": "g",
        "role_mapping": {"viewer": ["v"]},
    }
    profile = validate_deployment_profile(document, distribution_root=dist)
    assert profile.identity.role_mapping == {"viewer": ("v",)}


def test_a_missing_role_file_is_a_profile_error(tmp_path: Path) -> None:
    from olf.profile import DeploymentProfileError, _validate_identity

    with pytest.raises(DeploymentProfileError, match="role_mapping: cannot read"):
        _validate_identity(
            {
                "issuer": "external",
                "issuer_url": "https://idp.example.com",
                "role_claim": "groups",
                "role_mapping": {"viewer": ["g"]},
            },
            source="p",
            distribution_root=tmp_path,
        )


def _github_profile(**changes: object) -> dict:
    document = _load_fixture("valid_github_identity.yaml")
    document["spec"]["identity"].update(changes)
    return document


def test_github_issuer_derives_the_groups_claim_and_org_qualified_role_mapping() -> None:
    profile = validate_deployment_profile(_load_fixture("valid_github_identity.yaml"))
    identity = profile.identity

    assert identity.role_claim == "groups"
    assert dict(identity.role_mapping) == {
        "platform-admin": ("OpenLakeForge:olf-platform-admins",),
        "data-engineer": ("OpenLakeForge:olf-data-engineers",),
        "analyst": ("OpenLakeForge:olf-analysts",),
        "viewer": ("OpenLakeForge:olf-viewers",),
    }
    ref = identity.github.oauth_app_secret_ref
    assert (ref.name, ref.client_id_key, ref.client_secret_key) == ("github-oauth-app", "client-id", "client-secret")
    assert json.loads(resolve_topology(profile).render_json())["identity"]["github"]["organization"] == "OpenLakeForge"


def test_a_role_may_have_several_teams() -> None:
    github = _github_profile()["spec"]["identity"]["github"] | {"teams": {"viewer": ["a", "b"]}}

    identity = validate_deployment_profile(_github_profile(github=github)).identity

    assert identity.role_mapping["viewer"] == ("OpenLakeForge:a", "OpenLakeForge:b")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"organization": ""}, "organization"),
        ({"teams": {}}, "non-empty object"),
        ({"teams": {"owner": ["x"]}}, "unknown role 'owner'"),
        ({"teams": {"viewer": []}}, "non-empty list"),
        ({"teams": {"viewer": ["a", "a"]}}, "repeat a team slug"),
        ({"teams": {"viewer": ["a"], "analyst": ["a"]}}, "more than one role"),
        ({"oauth_app_secret_ref": {"name": "Bad_Name"}}, "oauth_app_secret_ref.name"),
        ({"oauth_app_secret_ref": {"name": "s", "client_id_key": "k", "client_secret_key": "k"}}, "must differ"),
        ({"oauth_app_secret_ref": {"name": "s", "client_secret": "literal"}}, "must not contain"),
    ],
)
def test_github_fails_closed(change: dict, match: str) -> None:
    github = _github_profile()["spec"]["identity"]["github"] | change
    with pytest.raises(DeploymentProfileError, match=match):
        validate_deployment_profile(_github_profile(github=github))


@pytest.mark.parametrize(
    "extra",
    [
        {"issuer_url": "https://login.example.com/"},
        {"role_claim": "groups"},
        {"role_mapping": {"viewer": ["v"]}},
        {"client_ids": {"trino": "t"}},
        {"smtp": {"host": "smtp.example.com", "port": 587, "from_address": "noreply@example.com"}},
        {"bootstrap_admins": ["ada@example.org"]},
    ],
)
def test_github_issuer_rejects_what_it_derives_or_does_not_use(extra: dict) -> None:
    with pytest.raises(DeploymentProfileError):
        validate_deployment_profile(_github_profile(**extra))


def test_github_block_and_issuer_go_together() -> None:
    document = _github_profile()
    del document["spec"]["identity"]["github"]
    with pytest.raises(DeploymentProfileError, match="requires 'github'"):
        validate_deployment_profile(document)

    with pytest.raises(DeploymentProfileError, match="issuer must be one of"):
        validate_deployment_profile(_github_profile(issuer="keycloak"))


def test_team_slugs_are_lowercased_but_the_organization_keeps_its_login_case() -> None:
    github = _github_profile()["spec"]["identity"]["github"] | {"teams": {"analyst": ["Olf-Analysts"]}}
    identity = validate_deployment_profile(_github_profile(github=github)).identity
    assert identity.github.organization == "OpenLakeForge"
    assert identity.role_mapping["analyst"] == ("OpenLakeForge:olf-analysts",)

    for teams, match in (
        ({"viewer": ["Analysts", "analysts"]}, "repeat a team slug"),
        ({"viewer": ["Analysts"], "analyst": ["analysts"]}, "more than one role"),
    ):
        with pytest.raises(DeploymentProfileError, match=match):
            validate_deployment_profile(_github_profile(github=github | {"teams": teams}))


@pytest.mark.parametrize("slug", ["Data_Engineering", "a.b", "-a", "a--b"])
def test_github_team_slugs_must_be_canonical(slug: str) -> None:
    github = _github_profile()["spec"]["identity"]["github"] | {"teams": {"viewer": [slug]}}
    with pytest.raises(DeploymentProfileError, match="slug as GitHub shows it"):
        validate_deployment_profile(_github_profile(github=github))


def test_plain_http_issuer_is_accepted_on_a_localhost_domain_of_the_local_provider() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["access"] = {"issuer": "none"}

    assert resolve_topology(validate_deployment_profile(document)).access == AccessSpec("olf.localhost", "none")


@pytest.mark.parametrize(
    ("provider", "base_domain"),
    [
        ("local", "olf.example.com"),
        ("local", "localhost.example.com"),
        ("local", "olf.localhost.evil.com"),
        ("aws", "olf.localhost"),
        ("azure", "olf.localhost"),
    ],
)
def test_plain_http_issuer_is_rejected_outside_local_localhost(provider: str, base_domain: str) -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["provider"] = {"type": provider, **({"region": "x"} if provider != "local" else {})}
    document["spec"]["access"] = {"issuer": "none", "base_domain": base_domain}

    with pytest.raises(DeploymentProfileError, match="issuer 'none' .* requires provider 'local'"):
        validate_deployment_profile(document)


def test_github_host_is_optional_and_must_be_a_bare_hostname() -> None:
    github = _github_profile()["spec"]["identity"]["github"]
    assert validate_deployment_profile(_github_profile(github=github | {"host": "ghe.example.com"})) is not None
    for bad in ("https://ghe.example.com", "ghe.example.com/api", 3):
        with pytest.raises(DeploymentProfileError, match="github.host"):
            validate_deployment_profile(_github_profile(github=github | {"host": bad}))


@pytest.mark.parametrize(
    ("provider", "access"),
    [("aws", None), ("azure", None), ("local", {"base_domain": "olf.example.com", "issuer": "letsencrypt"})],
)
def test_authentication_off_is_rejected_outside_local_localhost(provider: str, access: dict | None) -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"]["provider"] = {"type": provider, **({"region": "x"} if provider != "local" else {})}
    document["spec"].pop("access", None)
    if access:
        document["spec"]["access"] = access

    with pytest.raises(DeploymentProfileError, match="without spec.identity .* requires provider 'local'"):
        validate_deployment_profile(document)


def test_authentication_off_is_allowed_on_local_with_or_without_access() -> None:
    document = _load_fixture("valid_slim_local.yaml")
    document["spec"].pop("identity", None)
    document["spec"].pop("access", None)
    assert validate_deployment_profile(document).identity is None
    document["spec"]["access"] = {}
    assert validate_deployment_profile(document).identity is None
