from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from olf.contracts import build_contract_env
from olf.profile import StageName, resolve_topology, validate_deployment_profile
from olf.provider_contracts import ProviderContractError, aws_catalog_name, parse_provider_contracts

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((REPO_ROOT / "docs/schema/provider-contracts.schema.json").read_text())


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _synthetic_multi_capability_contract() -> dict:
    """A hand-assembled v3 contract exercising two governed stages plus one
    analytics-only stage - deliberately NOT a captured `terraform output -json
    provider_contracts`, unlike every other fixture in this file.

    No committed fixture combination has this shape: `local-provider-
    contracts-v3.json` (the only fixture whose contracts.tf root actually
    emits `pipeline_service_name`/`dashboard_service_name` - see
    infra/terraform/environments/local/contracts.tf) governs no stage at all,
    and `aws-provider-contracts-v3.json` is a hand-authored capture standing
    in for `aws-poc/contracts.tf`, which does not emit either field yet (that
    adapter work is deliberately still open in #131). Building this input
    from local's dev stage - the one root that is honest about these fields -
    keeps the test from asserting behaviour no real provider produces.
    """
    base = _fixture("local-provider-contracts-v3.json")
    base["shared"]["governance_service"] = {
        "ref": "shared/governance_service",
        "implementation": "governance.openmetadata",
    }

    dev = base["stages"]["dev"]
    dev["governance"] = {"service_ref": "shared/governance_service", "endpoint_ref": "stage/dev/endpoints/governance"}
    dev["reporting"] = {
        "service_ref": "stage/dev/reporting",
        "endpoint_ref": "stage/dev/endpoints/reporting",
        "dashboard_service_name": "superset_dev",
    }
    dev["endpoints"]["governance"] = "stage/dev/endpoints/governance"
    dev["endpoints"]["reporting"] = "stage/dev/endpoints/reporting"

    # uat: governed like dev, but analytics-off - cloned from dev (post-
    # governance) by renaming every dev-scoped token, then stripped of the
    # reporting capability it would otherwise inherit.
    uat = json.loads(json.dumps(dev).replace("dev", "uat"))
    del uat["reporting"]
    del uat["endpoints"]["reporting"]

    # prod: analytics-on, governance-off - the mirror image of uat, proving
    # `analytics_stages` and `governed_stages` diverge in both directions.
    prod = base["stages"]["prod"]
    prod["reporting"] = {
        "service_ref": "stage/prod/reporting",
        "endpoint_ref": "stage/prod/endpoints/reporting",
        "dashboard_service_name": "superset_prod",
    }
    prod["endpoints"]["reporting"] = "stage/prod/endpoints/reporting"

    base["stages"] = {"dev": dev, "uat": uat, "prod": prod}
    return base


def _topology(contract: dict):
    stages = {
        name: {
            "enabled": True,
            "capabilities": {
                "analytics": "reporting" in stage,
                "governance": "governance" in stage,
            },
        }
        for name, stage in contract["stages"].items()
    }
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
                    "stages": stages,
                },
            }
        )
    )


@pytest.mark.parametrize(
    "fixture_name",
    ["local-provider-contracts-v3.json", "azure-provider-contracts-v3.json", "aws-provider-contracts-v3.json"],
)
def test_v3_provider_fixtures_match_the_strict_schema_and_topology(fixture_name: str) -> None:
    contract = _fixture(fixture_name)

    jsonschema.validate(contract, SCHEMA)
    parsed = parse_provider_contracts(contract, _topology(contract))

    assert {stage.value for stage in parsed.stages} == set(contract["stages"])


def test_aws_fixture_preserves_logical_names_under_one_shared_glue_catalog() -> None:
    """This account's Glue service refuses to create a custom catalog per
    stage, so every stage shares the account's one default catalog
    (glue_catalog_id identical across stages) - `catalog_name` is what
    stays distinct, since it becomes each stage's physical database-name
    prefix rather than a catalog identity."""
    contract = _fixture("aws-provider-contracts-v3.json")
    parsed = parse_provider_contracts(contract, _topology(contract))

    dev = parsed.for_stage("dev")
    prod = parsed.for_stage("prod")

    assert dev.catalog["catalog_name"] == "lakehouse_dev"
    assert prod.catalog["catalog_name"] == "lakehouse_prod"
    assert dev.catalog["glue_catalog_id"] == "123456789012"
    assert dev.catalog["glue_catalog_id"] == prod.catalog["glue_catalog_id"]
    assert dev.catalog["catalog_namespace_model"] == prod.catalog["catalog_namespace_model"] == "medallion-owner"


def test_aws_catalog_name_is_stable_and_obeys_the_glue_limit() -> None:
    profile = "a" * 80
    name = aws_catalog_name(profile, "prod")

    assert len(name) == 64
    assert name.startswith("olf_")
    assert name.endswith("_prod_" + name[-8:])


def test_native_v3_requires_explicit_stage_for_environment_generation() -> None:
    contract = _fixture("local-provider-contracts-v3.json")

    with pytest.raises(ProviderContractError, match="requires an explicit stage"):
        build_contract_env({}, contract, repo_root=REPO_ROOT, topology=_topology(contract))


def test_stage_environment_exposes_only_its_selected_stage_values() -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)

    dev_exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage="dev")
    prod_exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage="prod")

    assert dev_exports["OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"] == "acme-dev-bronze"
    assert dev_exports["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"
    assert dev_exports["OPENLAKEFORGE_ARTIFACT_BASE_URI"].endswith("activations/dev")
    assert "prod" not in "\n".join(dev_exports.values())
    assert prod_exports["OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"] == "acme-prod-bronze"
    assert prod_exports["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_prod"
    assert "dev" not in "\n".join(prod_exports.values())


def test_stage_environment_carries_only_shared_and_its_own_routes() -> None:
    contract = _fixture("local-provider-contracts-v3.json")

    exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=_topology(contract), stage="prod")

    assert json.loads(exports["OPENLAKEFORGE_ACCESS_JSON"]) == {
        "issuer": "local-ca",
        "routes": {
            "shared/query": "https://trino.olf.localhost",
            "stage/prod/orchestration": "https://dagster.prod.olf.localhost",
        },
    }


def test_each_governed_stage_emits_lineage_under_its_own_pipeline_service() -> None:
    """OpenMetadata auto-creates pipelines named after the OpenLineage
    namespace, so each stage's must be its own root (#131)."""
    contract = _fixture("conformance-provider-contracts.json")
    topology = _topology(contract)

    for stage in ("dev", "prod"):
        exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage=stage)
        assert exports["OPENLINEAGE_NAMESPACE"] == f"dagster_{stage}"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda contract: contract.__setitem__("schema_version", "9.0.0"), "unsupported"),
        (lambda contract: contract["stages"].pop("prod"), "must equal enabled topology stages"),
        (
            lambda contract: contract["stages"].__setitem__("uat", copy.deepcopy(contract["stages"]["dev"])),
            "must equal enabled topology stages",
        ),
        (
            lambda contract: contract["stages"]["dev"]["query"].__setitem__("service_ref", "shared/missing"),
            "does not resolve",
        ),
        (
            lambda contract: contract["stages"]["dev"]["query"].__setitem__("catalog_ref", "stage/prod/catalog"),
            "cannot reference another stage",
        ),
        (
            lambda contract: contract["stages"]["prod"]["storage"]["gold"].__setitem__("physical_id", "acme-dev-gold"),
            "shared between stages",
        ),
        (lambda contract: contract["stages"]["dev"].__setitem__("unexpected", True), "unsupported fields"),
    ],
)
def test_v3_contracts_fail_closed_for_cross_stage_and_unknown_shapes(mutate, match: str) -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    mutate(contract)

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def test_v3_contract_rejects_a_missing_capability_binding() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    del contract["stages"]["dev"]["reporting"]

    with pytest.raises(ProviderContractError, match="reporting must match"):
        parse_provider_contracts(contract, topology)


def test_duplicate_bucket_name_across_stages_is_rejected() -> None:
    """A wiring mistake could give two stages distinct physical_id values but
    the same bucket_name; as_v2_environment_contract() exports bucket_name,
    so this must fail closed even though physical_id is already unique."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["prod"]["storage"]["bronze"]["bucket_name"] = "acme-dev-bronze"
    contract["stages"]["prod"]["storage"]["bronze"]["uri"] = "s3://acme-dev-bronze"

    with pytest.raises(ProviderContractError, match="shared between stages"):
        parse_provider_contracts(contract, topology)


def test_dev_reporting_cannot_reference_prod() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["reporting"]["service_ref"] = "stage/prod/reporting"

    with pytest.raises(ProviderContractError, match="reporting.service_ref must be 'stage/dev/reporting'"):
        parse_provider_contracts(contract, topology)


def test_dev_orchestration_endpoint_cannot_reference_prod() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["orchestration"]["endpoint_ref"] = "stage/prod/endpoints/orchestration"
    contract["stages"]["dev"]["endpoints"]["orchestration"] = "stage/prod/endpoints/orchestration"

    with pytest.raises(
        ProviderContractError, match="orchestration.endpoint_ref must be 'stage/dev/endpoints/orchestration'"
    ):
        parse_provider_contracts(contract, topology)


def test_dev_endpoints_catalog_cannot_reference_prod() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["endpoints"]["catalog"] = "stage/prod/catalog"

    with pytest.raises(ProviderContractError, match="must resolve the stage catalog"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    ("catalog_type", "catalog_provider", "match"),
    [
        ("nonsense", "polaris", "catalog_type must be one of"),
        ("rest", "polrais", "catalog_provider must be one of"),
        ("glue", "polaris", "does not match"),
        ("rest", "aws-glue", "does not match"),
    ],
)
def test_catalog_type_and_provider_must_be_a_known_matching_pair(
    catalog_type: str, catalog_provider: str, match: str
) -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["catalog_type"] = catalog_type
    contract["stages"]["dev"]["catalog"]["catalog_provider"] = catalog_provider

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def test_stage_storage_region_must_match_deployment_region() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["storage"]["region"] = "us-east-1"

    with pytest.raises(ProviderContractError, match="storage.region must match"):
        parse_provider_contracts(contract, topology)


def test_polaris_warehouse_must_match_its_own_physical_identity() -> None:
    """A Polaris catalog's warehouse is exported to the runtime by
    as_v2_environment_contract(); a stage could keep a distinct physical_id
    while pointing warehouse at another stage's catalog and reach the wrong
    Polaris warehouse despite passing the duplicate-catalog-identity check."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["prod"]["catalog"]["warehouse"] = "lakehouse_dev"

    with pytest.raises(ProviderContractError, match="warehouse must match its own physical identity"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize("capability_key", ["reporting", "governance"])
def test_endpoint_leak_is_rejected_when_the_capability_is_disabled(capability_key: str) -> None:
    """endpoints.<capability> must not survive when the capability binding
    itself is absent - otherwise a slim stage can still publish (and have a
    consumer resolve) a stray or cross-stage endpoint reference."""
    contract = _fixture("aws-provider-contracts-v3.json")
    contract["stages"]["prod"].pop(capability_key, None)
    contract["stages"]["prod"]["endpoints"][capability_key] = f"stage/prod/endpoints/orphan-{capability_key}"
    topology = _topology(contract)

    with pytest.raises(ProviderContractError, match=f"endpoints.{capability_key} must be absent"):
        parse_provider_contracts(contract, topology)


def test_query_endpoint_must_be_a_non_empty_string() -> None:
    """A schema-invalid non-string endpoint must fail closed with
    ProviderContractError at the parser boundary, not crash later in
    build_contract_env's endpoint.startswith("http://") with AttributeError."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["query"]["endpoint"] = 1

    with pytest.raises(ProviderContractError, match="query.endpoint must be a non-empty string"):
        parse_provider_contracts(contract, topology)


def test_glue_region_must_match_deployment_region() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["glue_region"] = "ap-south-1"

    with pytest.raises(ProviderContractError, match="glue_region must match"):
        parse_provider_contracts(contract, topology)


def test_local_provider_region_is_unconstrained() -> None:
    """Local deployments have no cloud region (deployment.region is null);
    the S3-compatible region string is a placeholder and must not be forced
    to agree with a region the deployment does not have."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    assert topology.region is None

    parse_provider_contracts(contract, topology)  # does not raise


def test_prod_stage_does_not_leak_dev_polaris_warehouse() -> None:
    """Regression for the two-pass default/apply ordering in build_contract_env:
    the first default pass pins POLARIS_WAREHOUSE to the dev default before
    contracts apply, and env.default() will not move an already-set value, so
    a PROD stage must be re-derived explicitly rather than keeping that stale
    default."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)

    prod_exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage="prod")

    assert prod_exports["POLARIS_WAREHOUSE"] == "lakehouse_prod"


def test_non_default_s3_endpoint_does_not_leak_the_generic_local_default() -> None:
    """Same two-pass staleness as the POLARIS_* aliases, but for
    AWS_ENDPOINT_URL_S3/AWS_S3_FORCE_PATH_STYLE: the first default pass pins
    them from the generic local endpoint default before the stage's real
    storage.endpoint/path_style_access apply, and env.default() (used for
    both in the non-aws_s3 branch) will not move an already-set value.
    Consumers like libs/bronze_csv.py would otherwise connect to the wrong
    SeaweedFS host or use the wrong addressing style for a stage with a
    genuinely different endpoint."""
    contract = _fixture("local-provider-contracts-v3.json")
    contract["stages"]["prod"]["storage"]["endpoint"] = "http://custom-seaweedfs:9000"
    contract["stages"]["prod"]["storage"]["path_style_access"] = False
    topology = _topology(contract)

    dev_exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage="dev")
    prod_exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage="prod")

    assert dev_exports["AWS_ENDPOINT_URL_S3"] == "http://seaweedfs-s3:8333"
    assert dev_exports["AWS_S3_FORCE_PATH_STYLE"] == "true"
    assert prod_exports["AWS_ENDPOINT_URL_S3"] == "http://custom-seaweedfs:9000"
    assert prod_exports["AWS_S3_FORCE_PATH_STYLE"] == "false"


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("bucket_name", 12345, "bucket_name must be a non-empty string"),
        ("bucket_name", ["not-hashable"], "bucket_name must be a non-empty string"),
        ("uri", ["not-a-string"], "uri must be a non-empty string"),
    ],
)
def test_storage_layer_fields_must_be_the_declared_type(field: str, value, match: str) -> None:
    """A malformed physical_id was already caught by _string(); bucket_name and
    uri were not, so a non-string value silently reached as_v2_environment_contract()
    (an empty/wrong-typed bucket) or crashed the cross-stage dedupe set with
    TypeError on an unhashable value."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["storage"]["bronze"][field] = value

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def test_query_service_ref_must_be_the_shared_query_service_specifically() -> None:
    """query.service_ref resolving to *any* shared/* binding (e.g. shared/ops_storage)
    would let a stage's query service masquerade as something else entirely; the
    documented shared-query exception is specifically shared.query."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["query"]["service_ref"] = "shared/ops_storage"
    contract["stages"]["dev"]["endpoints"]["query"] = "shared/ops_storage"

    with pytest.raises(ProviderContractError, match="must reference the shared query service"):
        parse_provider_contracts(contract, topology)


def test_catalog_service_ref_must_be_the_shared_catalog_service_specifically() -> None:
    """Same failure mode as query.service_ref: a Polaris stage could point its
    catalog service_ref at any resolvable shared/* binding and the StageContract
    would advertise that unrelated service as its catalog."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["service_ref"] = "shared/ops_storage"
    contract["stages"]["dev"]["endpoints"]["catalog"] = "shared/ops_storage"

    with pytest.raises(ProviderContractError, match="must reference the shared catalog service"):
        parse_provider_contracts(contract, topology)


def test_query_endpoint_must_agree_across_stages() -> None:
    """shared.query is one Trino service used by every stage, isolated by
    catalog rather than by endpoint; a stage that keeps service_ref: shared/query
    but declares a different endpoint would silently be routed to a different
    Trino host despite passing the shared-service-reference check."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["prod"]["query"]["endpoint"] = "http://other-trino:8080"

    with pytest.raises(ProviderContractError, match="query.endpoint must match the shared query service"):
        parse_provider_contracts(contract, topology)


def test_query_endpoint_must_match_the_shared_binding_even_if_every_stage_agrees() -> None:
    """The prior cross-stage-equality check only compared stages to each
    other; every stage agreeing on a value that still diverges from
    shared.query's own declared endpoint must still fail closed."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    for stage in contract["stages"].values():
        stage["query"]["endpoint"] = "http://other-trino:8080"

    with pytest.raises(ProviderContractError, match="query.endpoint must match the shared query service"):
        parse_provider_contracts(contract, topology)


def test_catalog_rest_uri_must_match_the_shared_catalog_services_endpoint() -> None:
    """as_v2_environment_contract() exports catalog.rest_uri to runtime
    consumers; a stage could keep a valid service_ref while pointing rest_uri
    at a different Polaris deployment entirely."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["rest_uri"] = "http://other-polaris:8181/api/catalog"

    with pytest.raises(ProviderContractError, match="rest_uri must match the shared catalog service's endpoint"):
        parse_provider_contracts(contract, topology)


def test_catalog_token_uri_must_be_derived_from_its_own_rest_uri() -> None:
    """floe.py renders catalog.token_uri as oauth2_server_uri alongside the
    client-credential Secret references; a stage could keep the correct,
    anchored rest_uri while pointing token_uri at an unrelated service and
    post credentials there instead."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["token_uri"] = "http://other-idp:9000/oauth/tokens"

    with pytest.raises(ProviderContractError, match="token_uri must share its own rest_uri's scheme and host:port"):
        parse_provider_contracts(contract, topology)


def test_catalog_token_uri_prefix_match_does_not_permit_a_lookalike_host() -> None:
    """A textual startswith() check treats http://polaris.attacker.test as
    prefixed by http://polaris, since the former literally begins with the
    latter's characters; comparing scheme+netloc via urlsplit instead must
    reject it even though the naive prefix check would have passed it."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["shared"]["catalog_service"]["endpoint"] = "http://polaris"
    contract["stages"]["dev"]["catalog"]["rest_uri"] = "http://polaris"
    contract["stages"]["dev"]["catalog"]["token_uri"] = "http://polaris.attacker.test/oauth"

    with pytest.raises(ProviderContractError, match="token_uri must share its own rest_uri's scheme and host:port"):
        parse_provider_contracts(contract, topology)


def test_relative_catalog_uris_do_not_bypass_same_origin_as_trivially_equal() -> None:
    """urlsplit("polaris") and urlsplit("oauth") both give an empty scheme
    and netloc, so _same_origin would have compared them as trivially equal
    without a prior absoluteness check - both operands must be validated as
    real http(s) URIs before their origins are ever compared."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["shared"]["catalog_service"]["endpoint"] = "polaris"
    contract["stages"]["dev"]["catalog"]["rest_uri"] = "polaris"
    contract["stages"]["dev"]["catalog"]["token_uri"] = "oauth"

    with pytest.raises(ProviderContractError, match="must be an absolute http:// or https:// URI"):
        parse_provider_contracts(contract, topology)


def test_non_string_token_uri_fails_closed_instead_of_crashing() -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["token_uri"] = 12345

    with pytest.raises(ProviderContractError, match="must be a non-empty string"):
        parse_provider_contracts(contract, topology)


def test_catalog_rest_uri_must_not_embed_credentials() -> None:
    """A credential-bearing authority (user:password@host) would still
    same-origin-match a matching anchor without embedded credentials, and
    build_contract_env()/floe.render_profile() would export and write those
    credentials into the generated EnvironmentProfile instead of keeping
    them as Kubernetes Secret references."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["shared"]["catalog_service"]["endpoint"] = "http://user:password@polaris:8181"
    contract["stages"]["dev"]["catalog"]["rest_uri"] = "http://user:password@polaris:8181"

    with pytest.raises(ProviderContractError, match="rest_uri must not embed credentials in its authority"):
        parse_provider_contracts(contract, topology)


def test_catalog_token_uri_must_not_embed_credentials() -> None:
    """Same as rest_uri: token_uri's own credential check must fire even
    when rest_uri itself is clean, since token_uri is validated
    independently after the same-origin comparison passes."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["token_uri"] = "http://user:password@polaris:8181/v1/oauth/tokens"

    with pytest.raises(ProviderContractError, match="token_uri must not embed credentials in its authority"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    ("endpoint", "match"),
    [
        ("seaweedfs:8333", "must be an absolute http:// or https:// URI"),
        (12345, "must be a non-empty string"),
    ],
)
def test_storage_endpoint_must_be_a_valid_absolute_uri(endpoint, match: str) -> None:
    """storage.endpoint was accepted unchanged and synchronized into
    AWS_ENDPOINT_URL_S3; a bare host:port or non-string value would hand
    boto3 an invalid endpoint in libs/bronze_csv.py instead of the contract
    failing closed here."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["storage"]["endpoint"] = endpoint

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def test_storage_layer_uri_must_address_its_own_bucket_name() -> None:
    """The published contract lists storage URI as a consumer-facing
    locator; a stage could keep a unique, correctly-deduped bucket_name
    while its uri names a different bucket entirely (even another stage's),
    routing a native consumer's reads/writes to the wrong bucket."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["storage"]["bronze"]["uri"] = "s3://acme-prod-bronze"

    with pytest.raises(ProviderContractError, match="storage.bronze.uri must address its own bucket_name"):
        parse_provider_contracts(contract, topology)


def test_ops_storage_artifact_base_uri_must_address_its_own_bucket_name() -> None:
    """Artifact commands publish through the bucket-name environment
    variable while Dagster reads OPENLAKEFORGE_ARTIFACT_BASE_URI directly;
    a mismatch between the two would make the same deployment write and
    read different buckets."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["shared"]["ops_storage"]["artifact_base_uri"] = "s3://another-bucket"

    with pytest.raises(ProviderContractError, match="artifact_base_uri must address its own bucket_name"):
        parse_provider_contracts(contract, topology)


def test_catalog_token_uri_is_required_whenever_rest_uri_is_supplied() -> None:
    """token_uri was only validated when present; the first environment-
    default pass leaves OPENLAKEFORGE_CATALOG_TOKEN_URI at the local
    Polaris default when omitted, and Floe renders that stale address as
    oauth2_server_uri regardless of the anchored, non-default rest_uri."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    del contract["stages"]["dev"]["catalog"]["token_uri"]

    with pytest.raises(ProviderContractError, match="token_uri is required when rest_uri is supplied"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    "port_suffix",
    [":invalid", ":99999"],
)
def test_catalog_uri_port_must_be_valid(port_suffix: str) -> None:
    """urlsplit() supplies a non-empty netloc even for an invalid or
    out-of-range port (its own .port property only raises when actually
    accessed); catalog reconciliation in commands/catalog.py evaluates
    .port later and would raise an uncaught ValueError instead of failing
    closed at the contract boundary."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["rest_uri"] = f"http://polaris{port_suffix}/api/catalog"

    with pytest.raises(ProviderContractError, match="rest_uri must have a valid TCP port"):
        parse_provider_contracts(contract, topology)


def test_glue_catalog_id_must_include_the_account_id_prefix() -> None:
    """A bare catalog *name* with no account-id at all (and no colon) is
    still rejected: GlueClient would otherwise receive it as an unusable
    CatalogId and only fail at the AWS API. A bare 12-digit account ID
    (no colon) is valid, though - see the next test - it names the
    account's own shared default catalog, the only kind this account's
    Glue service allows creating."""
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    bare_id = "olf_acme_data_dev"
    contract["stages"]["dev"]["catalog"]["glue_catalog_id"] = bare_id
    contract["stages"]["dev"]["catalog"]["physical_id"] = bare_id
    contract["stages"]["dev"]["catalog"]["glue_rest_warehouse"] = bare_id

    with pytest.raises(
        ProviderContractError, match=r"glue_catalog_id must be '<12-digit-account-id>\[:<catalog-name>\]'"
    ):
        parse_provider_contracts(contract, topology)


def test_glue_catalog_id_accepts_a_bare_account_id() -> None:
    """The account's shared default catalog (no custom-catalog suffix) is a
    valid glue_catalog_id, not just the legacy '<account-id>:<name>' form -
    both stages already use this bare form in the fixture."""
    contract = _fixture("aws-provider-contracts-v3.json")
    parsed = parse_provider_contracts(contract, _topology(contract))

    assert parsed.for_stage("dev").catalog["glue_catalog_id"] == "123456789012"


def test_storage_region_is_validated_even_when_topology_has_no_region() -> None:
    """The region-agreement check only ran when topology.region was not
    None, so for local (and any region-less cloud) profile, storage.region
    had no type validation at all - a non-string value would be stringified
    into OPENLAKEFORGE_STORAGE_REGION/AWS_REGION and fail S3/Floe
    operations downstream instead of at the contract boundary."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    assert topology.region is None
    contract["stages"]["dev"]["storage"]["region"] = []

    with pytest.raises(ProviderContractError, match="storage.region must be a non-empty string"):
        parse_provider_contracts(contract, topology)


def test_query_endpoint_port_must_be_within_the_valid_tcp_range() -> None:
    """The prior fix only checked the port was all-decimal-digits, so
    http://trino:65536 (out of the 1-65535 TCP range) still passed and was
    exported into the dbt profile and SQLAlchemy URI, failing every query
    client downstream instead of at the contract boundary."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    for stage in contract["stages"].values():
        stage["query"]["endpoint"] = "http://trino:65536"

    with pytest.raises(ProviderContractError, match="must be http://<host>:<port> with a valid TCP port"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    "port",
    ["invalid", 99999, 0, True, 8333.5],
)
def test_s3_service_port_must_be_a_valid_tcp_port(port) -> None:
    """artifact_store.artifact_storage_client() and the port-forward path in
    commands/artifacts.py both call int() on the exported value without
    their own validation; a malformed port must fail closed here instead of
    with an uncaught ValueError downstream."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["storage"]["s3_service_port"] = port

    with pytest.raises(ProviderContractError, match="s3_service_port must be a valid TCP port"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    ("endpoint", "match"),
    [
        ("https://trino.example:8443", "must be an http:// URI"),
        ("http://trino", "must be http://<host>:<port>"),
        ("http://trino:notaport", "must be http://<host>:<port>"),
    ],
)
def test_query_endpoint_must_be_a_shape_the_runtime_adapter_can_parse(endpoint: str, match: str) -> None:
    """olf.contracts._apply_provider_contracts only extracts host:port from an
    http:// endpoint; any other scheme or a missing/non-numeric port leaves
    OPENLAKEFORGE_QUERY_TRINO_HOST/PORT at their local defaults instead of
    routing to the declared service, silently."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    for stage in contract["stages"].values():
        stage["query"]["endpoint"] = endpoint

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def test_shared_binding_ref_must_be_its_own_canonical_name() -> None:
    """_parse_shared only checked that each binding's ref started with
    'shared/', not that it named the binding itself; a mistakenly wrong
    shared.ops_storage.ref would not resolve through the hardcoded
    activation.ops_storage_ref == 'shared/ops_storage' check, but
    environment adaptation indexes the ops_storage key directly and would
    mask the mismatch instead of failing closed."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["shared"]["ops_storage"]["ref"] = "shared/foundation"

    with pytest.raises(ProviderContractError, match="shared.ops_storage.ref must be 'shared/ops_storage'"):
        parse_provider_contracts(contract, topology)


def test_orchestration_service_ref_cannot_be_another_same_stage_binding() -> None:
    """The prior stage/<name>/* prefix check accepted ANY same-stage binding,
    so orchestration.service_ref could point at the stage's own catalog and
    the StageContract would advertise that catalog as its orchestration
    service - ADR 0011 classifies orchestration as a per-stage service with
    one canonical name, not a free-form same-stage path."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["orchestration"]["service_ref"] = "stage/dev/catalog"

    with pytest.raises(ProviderContractError, match="orchestration.service_ref must be 'stage/dev/orchestration'"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    "service_ref",
    ["shared/ops_storage", "stage/dev/orchestration"],
)
def test_reporting_service_ref_cannot_be_shared_or_another_same_stage_binding(service_ref: str) -> None:
    """reporting.service_ref previously accepted any resolvable shared/*
    binding via _stage_or_shared_reference, contradicting ADR 0011's
    classification of reporting as per-stage only (never shared) - and, like
    orchestration, also accepted another same-stage binding entirely."""
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["reporting"]["service_ref"] = service_ref

    with pytest.raises(ProviderContractError, match="reporting.service_ref must be 'stage/dev/reporting'"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    ("fixture_name", "catalog_type", "catalog_provider"),
    [
        ("local-provider-contracts-v3.json", "glue", "aws-glue"),
        ("aws-provider-contracts-v3.json", "rest", "polaris"),
    ],
)
def test_catalog_provider_must_match_the_deployment_topology_provider(
    fixture_name: str, catalog_type: str, catalog_provider: str
) -> None:
    """catalog_type/catalog_provider pairing alone doesn't stop a local/Azure
    topology from accepting a well-formed Glue catalog, or an AWS topology
    from accepting Polaris if a catalog_service happens to be supplied - ADR
    0010 binds Polaris to local/Azure and Glue to AWS specifically."""
    contract = _fixture(fixture_name)
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["catalog_type"] = catalog_type
    contract["stages"]["dev"]["catalog"]["catalog_provider"] = catalog_provider
    if catalog_provider == "polaris":
        contract["stages"]["dev"]["catalog"]["service_ref"] = "shared/catalog_service"

    with pytest.raises(ProviderContractError, match="does not match DeploymentTopology.provider"):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    ("fixture_name", "implementation"),
    [
        ("aws-provider-contracts-v3.json", "storage.s3_compatible.seaweedfs"),
        ("local-provider-contracts-v3.json", "storage.aws_s3"),
    ],
)
def test_storage_implementation_must_match_the_deployment_topology_provider(
    fixture_name: str, implementation: str
) -> None:
    """storage.provider alone doesn't stop an AWS stage from retaining a
    SeaweedFS implementation (so build_contract_env's storage.aws_s3 branch
    at contracts.py never fires and local S3-compatible endpoint/credential
    defaults leak into an AWS deployment), or a local stage from claiming
    storage.aws_s3."""
    contract = _fixture(fixture_name)
    topology = _topology(contract)
    contract["stages"]["dev"]["storage"]["implementation"] = implementation

    with pytest.raises(ProviderContractError, match="storage.implementation must match DeploymentTopology.provider"):
        parse_provider_contracts(contract, topology)


def test_catalog_physical_id_must_be_a_non_empty_string() -> None:
    """catalog.physical_id feeds the cross-stage catalog_ids dedupe set in
    _parse_v3 without prior type validation, unlike storage physical_id, which
    already used _string(); an unhashable value there would crash with
    TypeError instead of failing closed."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["catalog"]["physical_id"] = ["not-hashable"]

    with pytest.raises(ProviderContractError, match="catalog.physical_id must be a non-empty string"):
        parse_provider_contracts(contract, topology)


def test_glue_rest_warehouse_must_match_its_own_glue_catalog_id() -> None:
    """as_v2_environment_contract() exports glue_rest_warehouse to the runtime;
    a stage could point glue_rest_warehouse at a different Glue catalog ID
    than its own glue_catalog_id (both stages now legitimately share the
    same glue_catalog_id - the account's one default catalog - so this can
    no longer be tested by cross-wiring another stage's value)."""
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["prod"]["catalog"]["glue_rest_warehouse"] = "999999999999"

    with pytest.raises(ProviderContractError, match="glue_rest_warehouse must match its own Glue catalog ID"):
        parse_provider_contracts(contract, topology)


def test_duplicate_namespace_across_stages_is_rejected() -> None:
    """Each stage is documented as owning its Kubernetes namespace; two stages
    sharing one namespace would let stage-specific orchestration and artifact
    operations target the same resource scope."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["prod"]["namespace"] = contract["stages"]["dev"]["namespace"]

    with pytest.raises(ProviderContractError, match="namespace .* is shared between stages"):
        parse_provider_contracts(contract, topology)


def test_governance_service_ref_must_be_the_shared_governance_service_specifically() -> None:
    """Same failure mode as query.service_ref and catalog.service_ref: a
    governance-enabled stage could point governance.service_ref at any
    resolvable shared/* binding instead of shared.governance_service."""
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["governance"]["service_ref"] = "shared/ops_storage"

    with pytest.raises(ProviderContractError, match="must reference the shared governance service"):
        parse_provider_contracts(contract, topology)


def test_two_governed_stages_each_expose_their_own_deterministic_service_roots() -> None:
    """#131: a contract declaring more than one governed stage must parse as
    that many entries, each with its own lakehouse_<stage>/dagster_<stage>/
    superset_<stage> service roots - not collapse to Terraform's single
    selected stage the way main.tf's governance_dagster_stage/
    governance_superset_stage still do for the shared instance's live
    connections. See _synthetic_multi_capability_contract for why this input
    is built rather than read from a captured fixture."""
    contract = _synthetic_multi_capability_contract()
    topology = _topology(contract)

    parsed = parse_provider_contracts(contract, topology)

    assert {stage.value for stage in parsed.governed_stages} == {"dev", "uat"}
    dev = parsed.governed_stages[StageName.DEV]
    uat = parsed.governed_stages[StageName.UAT]
    assert dev.catalog["catalog_name"] == "lakehouse_dev"
    assert dev.orchestration["pipeline_service_name"] == "dagster_dev"
    assert dev.reporting["dashboard_service_name"] == "superset_dev"
    assert uat.catalog["catalog_name"] == "lakehouse_uat"
    assert uat.orchestration["pipeline_service_name"] == "dagster_uat"
    # uat is governed without analytics: its own deterministic roots are
    # still present even though it has no dashboard root to go with them.
    assert uat.reporting is None
    # prod is enabled but not governed: excluded from the governed map even
    # though it has its own reporting/orchestration service roots too.
    assert StageName.PROD not in parsed.governed_stages


@pytest.mark.parametrize(
    ("field", "path", "bad_value", "match"),
    [
        ("orchestration", "pipeline_service_name", "dagster-dev", "pipeline_service_name must be canonical"),
        ("reporting", "dashboard_service_name", "superset-dev", "dashboard_service_name must be canonical"),
    ],
)
def test_governed_stage_service_root_names_must_be_canonical(field: str, path: str, bad_value: str, match: str) -> None:
    """A stage-qualified service root that drifts from lakehouse_<stage>'s own
    naming convention would register an OpenMetadata service under a name a
    future bootstrap does not expect, silently orphaning it from the stage it
    describes."""
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"][field][path] = bad_value

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


@pytest.mark.parametrize(
    ("principal", "match"),
    [
        (["not-hashable"], "principal must be a non-empty string"),
        ("", "principal must be a non-empty string"),
    ],
)
def test_runtime_identity_principal_must_be_a_non_empty_string(principal, match: str) -> None:
    """runtime_identity.principal feeds the cross-stage principals dedupe set
    in _parse_v3 without prior type validation; an unhashable value would
    crash with TypeError, and an empty string would pass a single-stage
    topology's dedupe check entirely."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["stages"]["dev"]["runtime_identity"]["principal"] = principal

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def test_glue_stage_does_not_leak_local_polaris_token_and_scope_aliases() -> None:
    """The POLARIS_* compatibility aliases were re-derived before the is_glue
    normalization cleared OPENLAKEFORGE_CATALOG_TOKEN_URI/OAUTH_SCOPE, so an
    AWS/Glue stage exported the local Polaris dev defaults through
    POLARIS_TOKEN_URI/POLARIS_OAUTH_SCOPE while the canonical variables were
    correctly blank."""
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)

    exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=topology, stage="dev")

    assert exports["OPENLAKEFORGE_CATALOG_TOKEN_URI"] == ""
    assert exports["POLARIS_TOKEN_URI"] == ""
    assert exports["OPENLAKEFORGE_CATALOG_OAUTH_SCOPE"] == ""
    assert exports["POLARIS_OAUTH_SCOPE"] == ""


def test_v2_contract_lifts_to_legacy_dev_without_changing_environment_output() -> None:
    contract = _fixture("local-provider-contracts.json")
    parsed = parse_provider_contracts(contract)
    exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT)

    assert parsed.compatibility_v2 is True
    assert parsed.for_stage().name.value == "dev"
    assert exports["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"


def _binding(contract: dict, stage: str) -> str:
    from olf.deployment.activation import provider_binding_digest
    from olf.profile import StageName

    return provider_binding_digest(contract, topology=_topology(contract), stage=StageName(stage))


def test_binding_digest_tracks_nested_contract_values() -> None:
    """Parsed contracts freeze nested bindings; hashing their field names hides a rebind."""
    contract = _fixture("aws-provider-contracts-v3.json")
    mutated = copy.deepcopy(contract)

    def rename(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and "bronze" in value:
                    node[key] = value.replace("bronze", "other-bucket")
                else:
                    rename(value)

    rename(mutated["stages"]["dev"]["storage"])

    assert _binding(contract, "dev") != _binding(mutated, "dev")
    # A stage nothing touched must not be disturbed by another stage's rebind.
    assert _binding(contract, "prod") == _binding(mutated, "prod")


def test_binding_digest_is_stable_for_an_unchanged_contract() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")

    assert _binding(contract, "dev") == _binding(copy.deepcopy(contract), "dev")


def test_stage_carries_its_contracted_code_locations() -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "sales", "definitions_module": "lakehouse_code.sales_definitions"},
        {"name": "finance", "definitions_module": "lakehouse_code.finance_definitions"},
    ]

    parsed = parse_provider_contracts(contract, _topology(contract))

    assert [(location.name, location.definitions_module) for location in parsed.for_stage("dev").code_locations] == [
        ("sales", "lakehouse_code.sales_definitions"),
        ("finance", "lakehouse_code.finance_definitions"),
    ]


def test_contract_emitted_before_the_field_resolves_the_merged_default() -> None:
    """A platform last applied before #185 has a persisted 3.0.0 contract with
    no code_locations. Rejecting it would make a code commit demand a platform
    apply -- ADR 0002 keeps those two lifecycles apart -- so it resolves to the
    single merged location that state is actually running."""
    contract = _fixture("local-provider-contracts-v3.json")
    del contract["stages"]["dev"]["orchestration"]["code_locations"]

    jsonschema.validate(contract, SCHEMA)
    parsed = parse_provider_contracts(contract, _topology(contract))

    assert [(location.name, location.definitions_module) for location in parsed.for_stage("dev").code_locations] == [
        ("openlakeforge-dagster", "lakehouse_code.definitions")
    ]


def test_a_contract_predating_the_field_still_builds_its_runtime_environment() -> None:
    """`olf deploy`'s artifacts phase reads the same contract; it must not start
    failing on Terraform state nobody has re-applied."""
    contract = _fixture("local-provider-contracts-v3.json")
    for stage in contract["stages"].values():
        del stage["orchestration"]["code_locations"]

    exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=_topology(contract), stage="dev")

    assert exports["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"


def test_a_contract_predating_the_field_still_digests_stably() -> None:
    """The resolved default is read through the property, never written back into
    the parsed binding, so a legacy contract keeps hashing to one value however
    often it is read -- a CLI upgrade alone does not move it."""
    contract = _fixture("aws-provider-contracts-v3.json")
    del contract["stages"]["dev"]["orchestration"]["code_locations"]

    assert _binding(contract, "dev") == _binding(copy.deepcopy(contract), "dev")


@pytest.mark.parametrize(
    ("code_locations", "match"),
    [
        ([], "must be a non-empty list"),
        ("openlakeforge-dagster", "must be a non-empty list"),
        ([{"name": "sales"}], r"code_locations\[0\] is missing required fields"),
        ([{"name": "sales", "definitions_module": "m", "port": 3030}], "contains unsupported fields"),
        # dagster-user-deployments names the Service after the deployment, so
        # a name the API server would reject is unusable as an in-cluster host.
        ([{"name": "Sales Domain", "definitions_module": "m"}], "must be an RFC 1035 label"),
        # A Service name may not start with a digit: DNS-1123 allows it, RFC 1035
        # does not, and the API server rejects it mid-rollout rather than here.
        ([{"name": "1sales", "definitions_module": "m"}], "must be an RFC 1035 label"),
        # `$` alone would match before a trailing newline and let it through.
        ([{"name": "sales\n", "definitions_module": "m"}], "must be an RFC 1035 label"),
        ([{"name": "sales", "definitions_module": "m\n"}], "must be a dotted Python module path"),
        ([{"name": "sales", "definitions_module": "lakehouse_code/definitions"}], "dotted Python module path"),
        (
            [
                {"name": "sales", "definitions_module": "a"},
                {"name": "sales", "definitions_module": "b"},
            ],
            "declares the code location 'sales' twice",
        ),
    ],
)
def test_malformed_code_locations_fail_closed(code_locations: object, match: str) -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    contract["stages"]["dev"]["orchestration"]["code_locations"] = code_locations

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, _topology(contract))


def test_two_stages_may_reuse_one_code_location_name() -> None:
    """Each stage runs in its own namespace, so the same name is not the kind of
    collision a shared bucket or catalog identity is - and every root hands
    every stage the same default list."""
    contract = _fixture("local-provider-contracts-v3.json")

    parsed = parse_provider_contracts(contract, _topology(contract))

    assert [location.name for location in parsed.for_stage("dev").code_locations] == [
        location.name for location in parsed.for_stage("prod").code_locations
    ]


def test_renaming_a_code_location_moves_the_binding_digest() -> None:
    """Activation renders the user deployments from this list, so a rename has
    to force a rollout rather than leaving the previous Services in place."""
    contract = _fixture("aws-provider-contracts-v3.json")
    renamed = copy.deepcopy(contract)
    renamed["stages"]["dev"]["orchestration"]["code_locations"][0]["name"] = "sales"

    assert _binding(contract, "dev") != _binding(renamed, "dev")


def test_deployment_profile_name_mismatch_names_both_values() -> None:
    """The nightly full e2e failed on this check for eleven runs: `olf deploy
    --profile full` records the "legacy" shorthand topology while `olf e2e
    run` re-resolved the project profile. A message without the two names
    sends the reader to the CI log rather than to the two profiles that
    disagree."""
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["deployment"]["profile_name"] = "legacy"

    with pytest.raises(ProviderContractError) as excinfo:
        parse_provider_contracts(contract, topology)

    message = str(excinfo.value)
    assert "'legacy'" in message
    assert repr(topology.profile_name) in message


def test_deployment_region_mismatch_names_both_values() -> None:
    contract = _fixture("aws-provider-contracts-v3.json")
    topology = _topology(contract)
    contract["deployment"]["region"] = "eu-west-9"

    with pytest.raises(ProviderContractError) as excinfo:
        parse_provider_contracts(contract, topology)

    message = str(excinfo.value)
    assert "'eu-west-9'" in message
    assert repr(topology.region) in message


def test_analytics_stages_are_tracked_separately_from_governed_stages() -> None:
    """#131: analytics and governance are independent per-stage capabilities
    (ADR 0011), so the dashboard roots OpenMetadata registers cannot be found
    by iterating the governed set. A stage with analytics and no governance
    owns the only `superset_<stage>` that exists; looping `governed_stages`
    for all three service types would skip it silently. See
    _synthetic_multi_capability_contract for why this input is built rather
    than read from a captured fixture."""
    contract = _synthetic_multi_capability_contract()
    topology = _topology(contract)

    parsed = parse_provider_contracts(contract, topology)

    analytics_only = set(parsed.analytics_stages) - set(parsed.governed_stages)
    assert analytics_only, (
        "the fixture no longer has an analytics stage without governance, so this "
        "test cannot show the two sets differ; pick another stage rather than deleting it."
    )
    for stage in analytics_only:
        reporting = parsed.analytics_stages[stage].reporting
        assert reporting is not None
        assert reporting["dashboard_service_name"] == f"superset_{stage.value}"


def test_local_access_routes_only_enabled_user_facing_services() -> None:
    """The captured local contract routes each stage's Dagster and the shared
    Trino, and nothing internal or disabled (no reporting, no governance)."""
    contract = _fixture("local-provider-contracts-v3.json")

    jsonschema.validate(contract, SCHEMA)
    parsed = parse_provider_contracts(contract, _topology(contract))

    routes = parsed.shared.values["access"]["routes"]
    assert {ref: route["url"] for ref, route in routes.items()} == {
        "shared/query": "https://trino.olf.localhost",
        "stage/dev/orchestration": "https://dagster.dev.olf.localhost",
        "stage/prod/orchestration": "https://dagster.prod.olf.localhost",
    }
    assert {route["exposure"] for route in routes.values()} == {"user-facing"}


def _route(ref: str, url: str) -> dict:
    return {ref: {"url": url, "enabled": True, "exposure": "user-facing"}}


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        # Internal endpoints cannot be published to users, by contract.
        (
            lambda access: access["routes"].update(_route("shared/metadata_database", "https://pg.olf.localhost")),
            "internal endpoint",
        ),
        (
            lambda access: access["routes"].update(_route("shared/ops_storage", "https://s3.olf.localhost")),
            "internal endpoint",
        ),
        # A disabled capability (no reporting on this fixture) has no service to route.
        (
            lambda access: access["routes"].update(_route("stage/dev/reporting", "https://superset.dev.olf.localhost")),
            "does not resolve",
        ),
        (
            lambda access: access["routes"].update(_route("stage/dev/code-server", "https://code.dev.olf.localhost")),
            "does not resolve",
        ),
        (lambda access: access.pop("issuer"), "declared together"),
        (lambda access: access.__setitem__("base_domain", "olf.example.com"), "does not match the profile"),
        (lambda access: access.__setitem__("issuer", "letsencrypt"), "must match the profile"),
        (
            lambda access: access["routes"]["stage/dev/orchestration"].__setitem__(
                "url", "http://dagster.dev.olf.localhost"
            ),
            "must be https",
        ),
        (
            lambda access: access["routes"]["stage/dev/orchestration"].__setitem__(
                "url", "https://dagster.example.com"
            ),
            "must be https",
        ),
        (
            lambda access: access["routes"].update(
                {
                    "shared/catalog_service": {
                        "url": "https://trino.olf.localhost",
                        "enabled": True,
                        "exposure": "internal",
                    }
                }
            ),
            "already routed",
        ),
        (lambda access: access["routes"]["shared/query"].__setitem__("exposure", "public"), "exposure must be"),
        (
            lambda access: [access.pop(field) for field in ("base_domain", "issuer", "tls_mode", "routes")],
            "access.ingress requires",
        ),
        (
            lambda access: access["routes"]["shared/query"].__setitem__("url", "https://DAGSTER.dev.olf.localhost:443"),
            "must be https",
        ),
        (
            lambda access: access["routes"]["shared/query"].__setitem__(
                "url", "https://dagster.dev.olf.localhost/trino"
            ),
            "must be https",
        ),
        (
            lambda access: access["routes"].update(
                {
                    "shared/catalog_service": {
                        "url": "https://TRINO.olf.localhost",
                        "enabled": True,
                        "exposure": "internal",
                    }
                }
            ),
            "already routed",
        ),
        # A stage route carries its own stage label; a shared route carries none.
        (
            lambda access: access["routes"]["stage/dev/orchestration"].__setitem__(
                "url", "https://dagster.prod.olf.localhost"
            ),
            "must be https",
        ),
        (
            lambda access: access["routes"]["stage/dev/orchestration"].__setitem__(
                "url", "https://dagster.olf.localhost"
            ),
            "must be https",
        ),
        (
            lambda access: access["routes"]["shared/query"].__setitem__("url", "https://trino.dev.olf.localhost"),
            "must be https",
        ),
        (
            lambda access: access["routes"]["shared/query"].__setitem__("url", "https://bad_host.olf.localhost"),
            "must be https",
        ),
        (
            lambda access: access["routes"]["shared/query"].__setitem__("url", "https://a..olf.localhost"),
            "must be https",
        ),
        (
            lambda access: access["routes"].update(
                {"shared/secrets": {"url": "https://vault.olf.localhost", "enabled": True, "exposure": "internal"}}
            ),
            "does not resolve",
        ),
        (lambda access: access["routes"]["shared/query"].__setitem__("enabled", "yes"), "enabled must be"),
        (lambda access: access.__setitem__("tls_mode", "passthrough"), "tls_mode must be one of"),
        (lambda access: access.__setitem__("tls_mode", ["ingress-terminated"]), "tls_mode"),
    ],
)
def test_access_ingress_contract_fails_closed(mutate, match: str) -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    mutate(contract["shared"]["access"])

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def _drop_identity_roles(contract: dict) -> None:
    del contract["shared"]["identity"]["roles"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_drop_identity_roles, "missing required field 'roles'"),
        (
            lambda contract: contract["shared"]["identity"]["roles"]["grants"]["query"].__setitem__("guest", "yes"),
            "unknown role 'guest'",
        ),
        (
            lambda contract: contract["shared"]["identity"]["roles"]["grants"].__setitem__("portal", {"viewer": "x"}),
            "unknown service 'portal'",
        ),
        (
            # analyst holds a grant its superior data-engineer lacks.
            lambda contract: contract["shared"]["identity"]["roles"]["grants"].__setitem__(
                "query", {"platform-admin": "yes", "analyst": "yes"}
            ),
            "query is not monotonic",
        ),
        (
            lambda contract: contract["shared"]["identity"]["roles"]["grants"]["reporting"].__setitem__(
                "analyst", "Admin"
            ),
            "differs from release/identity-roles.yaml",
        ),
        (
            lambda contract: contract["shared"]["identity"]["roles"]["precedence"].append("auditor"),
            "differs from release/identity-roles.yaml",
        ),
    ],
)
def test_identity_roles_fail_closed_unless_they_equal_the_release_role_model(mutate, match: str) -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    mutate(contract)

    with pytest.raises(ProviderContractError, match=match):
        parse_provider_contracts(contract, topology)


def test_identity_roles_fail_closed_when_the_release_role_model_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _fixture("local-provider-contracts-v3.json")
    topology = _topology(contract)
    monkeypatch.setenv("OLF_DISTRIBUTION_ROOT", str(tmp_path))

    with pytest.raises(ProviderContractError, match="cannot read the canonical role model"):
        parse_provider_contracts(contract, topology)
