"""Shared classification data for provider-contract conformance suites.

`test_provider_conformance.py` and `test_multi_stage_conformance.py` (#155)
both compare a `build_contract_env` result against a real captured provider
contract, and both need to know which emitted variables are stage-bound
(must equal the contract), derived from a stage-bound one, or gated on a
capability rather than a stage. Sharing one classification here is what
keeps the two suites from drifting apart on what a stage-carrying export is
-- duplicating the lists would let one suite's copy fall out of date with
the exports `olf/contracts.py` actually emits while the other still passed.
"""

from __future__ import annotations

import re
from typing import Any

from olf.deployment.context import stage_namespace
from olf.profile import StageName

MEDALLION_LAYERS = ("bronze", "silver", "gold")


def warehouse(stage: Any) -> str:
    return str(stage.catalog.get("warehouse") or stage.catalog["catalog_name"])


def bucket(stage: Any, layer: str) -> str:
    return str(stage.storage[layer]["bucket_name"])


# Every export whose value is one of the selected stage's contracted bindings,
# with the binding it must equal. Derived from the emit sites in
# `olf/contracts.py`, not from memory: each of these differs between stages on
# at least one provider, so each is a value a run reaches data or Trino
# through. `OPENLAKEFORGE_DBT_TRINO_USER` is the authentication boundary --
# Trino's catalog access rules tell stages apart by this principal.
# `warehouse` is Polaris-only; where a catalog does not name one the catalog
# name is the warehouse, which is what the emitter falls back to.
STAGE_BINDINGS: tuple[tuple[str, Any], ...] = (
    ("OPENLAKEFORGE_CONTRACT_STAGE", lambda stage: stage.name.value),
    ("OPENLAKEFORGE_KUBE_NAMESPACE", lambda stage: stage.namespace),
    ("OPENLAKEFORGE_CATALOG_NAME", lambda stage: str(stage.catalog["catalog_name"])),
    ("OPENMETADATA_CATALOG_DATABASE", lambda stage: str(stage.catalog["catalog_name"])),
    ("OPENLAKEFORGE_CATALOG_WAREHOUSE", warehouse),
    ("POLARIS_WAREHOUSE", warehouse),
    ("OPENLAKEFORGE_QUERY_TRINO_CATALOG", lambda stage: str(stage.query["catalog_name"])),
    ("OPENLAKEFORGE_DBT_TRINO_USER", lambda stage: str(stage.runtime_identity["principal"])),
    ("OPENLAKEFORGE_STORAGE_BUCKET", lambda stage: bucket(stage, "bronze")),
    ("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET", lambda stage: bucket(stage, "bronze")),
    ("OPENLAKEFORGE_STORAGE_SILVER_BUCKET", lambda stage: bucket(stage, "silver")),
    ("OPENLAKEFORGE_STORAGE_GOLD_BUCKET", lambda stage: bucket(stage, "gold")),
)

# Credentials by Secret reference, where the provider hands a stage static
# ones (local's SeaweedFS and Polaris principals; AWS and Azure use workload
# identity and carry none). Another stage's Secret authenticates as that
# stage, whatever the bindings above say.
STAGE_CREDENTIAL_BINDINGS: tuple[tuple[str, Any], ...] = (
    ("OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME", lambda stage: str(stage.storage["credentials_secret_name"])),
    (
        "OPENLAKEFORGE_CATALOG_DEPLOYER_CREDENTIALS_SECRET_NAME",
        lambda stage: str(stage.catalog["deployer_credentials_secret_name"]),
    ),
    (
        "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME",
        lambda stage: str(stage.catalog["floe_credentials_secret_name"]),
    ),
)

# Exports that must land under the stage's own activation prefix, mapped to the
# suffix each adds. Stages share one ops bucket, so the prefix is the only
# thing keeping one stage's manifests, logs and run artifacts out of another's.
ACTIVATION_URIS = {
    "OPENLAKEFORGE_ARTIFACT_BASE_URI": "",
    "OPENLAKEFORGE_FLOE_MANIFEST_BASE_URI": "/floe/manifests",
    "OPENLAKEFORGE_FLOE_REPORT_BASE_URI": "/floe/reports",
    "OPENLAKEFORGE_LOG_BASE_URI": "/logs",
    "OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI": "/run-artifacts",
}

# Exports that vary by stage but are *built from* a binding above rather than
# equal to one -- catalog FQNs, the per-product namespace and schema JSON
# blobs, Glue's stage-qualified ids, and the SQLAlchemy URI that concatenates
# principal and catalog. Restating their derivation here would test the
# implementation; the stage-word scan is what holds them, and listing them
# means a new stage-carrying export is a deliberate classification rather than
# something nobody noticed.
STAGE_DERIVED_EXPORTS = frozenset(
    {
        "OPENLAKEFORGE_CATALOG_DATABASE_FQN",
        "OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID",
        "OPENLAKEFORGE_CATALOG_GLUE_REST_WAREHOUSE",
        "OPENLAKEFORGE_CATALOG_GOLD_NAMESPACES_JSON",
        "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON",
        "OPENLAKEFORGE_CATALOG_NAMESPACES_JSON",
        "OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX",
        "OPENLAKEFORGE_CATALOG_SILVER_NAMESPACES_JSON",
        "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON",
        "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI",
    }
)

# Exports that differ between stages because a stage turned a *capability* off
# (ADR 0011), not because they name a stage: an ungoverned stage gets no
# lineage endpoint and no ingestion-bot credential at all. Their values carry
# no stage identity, so there is nothing to compare them against here.
CAPABILITY_GATED_EXPORTS = frozenset(
    {
        "OPENLAKEFORGE_GOVERNANCE_ENABLED",
        "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
        "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
        "OPENLINEAGE_ENDPOINT",
        "OPENLINEAGE_NAMESPACE",
        "OPENLINEAGE_URL",
    }
)


def names_stage(value: str, stage_name: str) -> bool:
    """True when `value` mentions `stage_name` as a whole alphanumeric word.

    Scanning for the *stage name* rather than for a whole derived identity is
    what makes this catch names built out of one: `olf-prod-runtime` and
    `lakehouse_prod_sales_silver` both contain the word `prod`, while a scan
    keyed to `olf-prod` or `lakehouse_prod` misses both, because `-` and `_`
    keep them inside a single token.

    Splitting on every non-alphanumeric byte is safe here for the same reason
    it would not be for a whole identity: a provider that names DEV's bucket
    `acme-data` and PROD's `acme-data-prod` (AGENTS.md rule 2 delegates that
    choice) yields words `acme|data|bronze` for DEV, which contains no other
    stage's name.
    """
    return stage_name in re.split(r"[^A-Za-z0-9]+", value)


def logical_identities(stage_name: StageName, stage: Any) -> tuple[tuple[str, str, str], ...]:
    """The names shared, provider-neutral code derives from the stage alone.

    Each has exactly one correct value per stage, on every provider, because
    olf itself computes the expected one. Physical object-store and catalog
    naming is *not* here: rule 2 delegates it to the provider contract, so an
    account-derived bucket name is correct rather than a violation.
    """
    return (
        ("namespace", str(stage.namespace), stage_namespace(stage_name)),
        ("catalog.catalog_name", str(stage.catalog["catalog_name"]), f"lakehouse_{stage_name.value}"),
        ("query.catalog_name", str(stage.query["catalog_name"]), f"lakehouse_{stage_name.value}"),
        ("activation.prefix", str(stage.activation["prefix"]), f"activations/{stage_name.value}"),
    )
