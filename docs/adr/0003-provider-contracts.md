# ADR 0003: Provider contracts are the portability boundary

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS ✔ · Azure ✔ (every root emits v3, #114) |

## Context

OpenLakeForge runs the same data project on local, AWS and Azure providers.
That claim fails if a runtime reads provider details directly, or if a
multi-stage profile requires each consumer to infer physical storage and
catalog names independently.

## Decision

**D1. Terraform owns physical provider values and exports provider contracts;
`olf` parses them.** `olf` owns parsing, fail-closed validation and runtime
environment generation. Product descriptors stay provider-neutral. A new
provider or stage-scoped capability starts by extending v3 and its fixtures,
then implementing adapters; a provider never invents a competing wire shape.
Enforced by: `tools/olf/olf/common/provider_contracts/`.

**D2. Provider contract v3 is one `shared` family plus a `stages` mapping.** The
stages equal the enabled DEV, optional UAT and PROD stages of the resolved
`DeploymentTopology`. Shared bindings are declared once; a stage may refer
only to a shared binding or to its own stage binding. A v3 payload resolves
only for an explicitly selected stage. Enforced by:
`tools/olf/olf/common/provider_contracts/_v3.py` and `build_contract_env` in
`tools/olf/olf/infra/contracts.py`.

**D3. Every stage owns distinct bindings, and contracts carry references, never
credentials.** Each stage has its own Bronze, Silver and Gold storage
identities, catalog binding, query binding, runtime identity, orchestration
endpoint and activation prefix in shared ops storage. Reporting and
governance bindings are present exactly when that stage capability is
enabled. Secret and workload-identity references appear; credential values
never do. Enforced by: the v3 parser and the secret-field rule of `olf check
contracts`.

**D4. Logical SQL identity is provider-neutral:
`lakehouse_<stage>.<owner>_<layer>.<table>`.** Catalog and object-store
physical identities are derived by the provider adapter. Enforced by:
`resolve_physical_names` in `packages/core/openlakeforge_core/inventory.py`.

**D5. Consumers branch on `catalog_type`, never on a provider name**, and do not
assume Polaris REST or OAuth fields are present. This is the rule that lets
the Glue implementation exist without touching every consumer, and the one a
change tested only locally most easily breaks. Enforced by: the AWS (Glue)
fixture in `tools/olf/tests/common/test_provider_contracts.py`.

**D6. A capability's contract shape is named before its second implementation
exists.** `secrets`, `identity`, `access` and `observability` have contract
shapes although not every provider implements them, so later hardening is
not a rewrite. Enforced by: `docs/architecture/provider-contracts.md` and the
v3 schema.

**D7. `olf` parses v3 only.** A `2.0.0` contract, left by a pre-v0.3 deploy, is
refused with the supported version and the remedy: migrate the pre-v0.3
shared `lakehouse` namespace's data by hand, or start over with
`--allow-stage-removal`, before redeploying the platform phase. The `2.0.0`
shape is still exported to runtimes by `as_v2_environment_contract`. Enforced
by: `SUPPORTED_SCHEMA_VERSIONS` in `tools/olf/olf/common/contract_validation.py`.

**D8. Invalid contracts fail closed.** Unknown versions and fields, missing or
extra stages, cross-stage bindings, duplicate physical identities (storage
bucket names included, not only physical IDs), unknown or mismatched catalog
type and provider pairs, and provider, region or topology mismatches are
refused. Enforced by: `tools/olf/tests/common/test_provider_contracts.py`
over the local, AWS and Azure fixtures, and `olf check contracts` over the
Terraform surface.

**D9. Query isolation follows the stage where Trino is per stage.** The local
root runs one Trino per stage (ADR 0019 section 9): each stage carries its own
query endpoint, and `shared.query` names the selected stage's. On AWS and
Azure, query connectivity is a deliberate exception to stage isolation:
`shared.query` is one service used by every enabled stage, isolated by
catalog rather than by endpoint. Enforced by: `stage_query_endpoints` in
`infra/terraform/environments/local/contracts.tf` and the v3 parser's
query-reference rule.

**D10. `query.catalog_name` is the Trino catalog name and equals
`lakehouse_<stage>`.** On a Polaris-backed root (local, Azure) it is a
separate physical catalog per stage; on AWS it is a logical alias over the
account's one shared Glue catalog (ADR 0010). The aliasing and prefix
mechanics are in [Provider Contracts](../architecture/provider-contracts.md#logical-and-physical-identity).
Enforced by: `expected_catalog` in `tools/olf/olf/common/provider_contracts/_v3.py`.

**D11. `shared.artifact_registry.image_repository` is an optional,
provider-owned fallback for project images.** It never includes a tag, digest
or credentials. An explicit project repository takes precedence, and a
contract may omit the field. The adapter that offers it owns publishing and
workload availability; project descriptors stay provider-neutral. Enforced
by: the v3 parser; the field rules are in
[Provider Contracts](../architecture/provider-contracts.md#sharedartifact_registry).

## Consequences

- Every deploy, e2e and artifact code path resolves and passes a stage
  explicitly; nothing defaults to DEV.
- Platform state written before v0.3 is unsupported; there is no automatic
  migration from a `2.0.0` contract.
- AWS and Azure share one Trino across stages until their cloud milestone
  (D9), so their stage boundary there is the catalog, not the network.

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Runtimes reading provider details directly, or each consumer deriving physical names | Breaks portability and multi-stage profiles; every consumer would need a Glue branch |
| Keeping the v2 reader beside v3 | Kept pre-v0.3 state alive at the cost of a second parser; removed in #114 |

## History

- 2026-08-28 (#133, #114): New record for provider-contract v3, merging the
  former ADRs 0010 (provider-contract-first cloud readiness), 0011 (the
  Iceberg catalog contract allowing Glue) and 0012 (contract-driven hardening);
  v2 stayed readable through an adapter.
- 2026-08-31 (#114): D10 changed: AWS's `lakehouse_<stage>` is a logical alias
  over one shared Glue catalog, not a physical catalog per stage.
- 2026-11-03 (#558): D11 added.
- 2026-11-06 (#114): D7 changed: the v2 reader is removed and a `2.0.0`
  contract is refused; the v2 export to runtimes stays.
- 2026-11-22 (#1385): D9 corrected: the local root runs one Trino per stage.
- 2026-11-22: Rewritten in the standard form; no decision changed.
