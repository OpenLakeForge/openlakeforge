# Agent and contributor guide

This is the working guide for anyone changing OpenLakeForge — human or coding
agent. It records what the repository actually enforces today. If a statement
here disagrees with the code, the code wins and this file is a bug.

`CLAUDE.md` points here. There is one copy of these rules on purpose.

## What this project is

OpenLakeForge is a cloud-agnostic, self-hostable lakehouse platform: open-source
components assembled on Kubernetes with Terraform and Helm. The data path is
CSV → Bronze → Floe validation → Silver Iceberg through Polaris → dbt-trino Gold
→ Trino → Superset, orchestrated by Dagster.

The audience is small data teams self-hosting a complete lakehouse. Runtime
footprint, onboarding friction, and recoverability are product features here,
not polish — a team without a platform engineer cannot absorb a stack that needs
one. `v0.2-alpha` is scoped around exactly that; see the roadmap.

## Non-goals

These are settled scope decisions, not gaps. A change that adds machinery for
one of them is out of scope, and a review finding that asks for it should be
closed with a pointer here rather than fixed.

| Not a goal | Why |
| --- | --- |
| Concurrent or re-entrant `olf` invocation | `olf` is a single-operator CLI: one invocation at a time, on a workstation or a CI runner. `olf deploy` runs its phases sequentially in one process and relies on that. Do not add locks, leases, atomic-rename dances, or TOCTOU guards to shared code. |
| Windows support | Deployment targets are Linux and macOS. |

The audience is a small data team without a platform engineer, and that is a
design constraint rather than a market description: machinery those teams will
never exercise is not free. It costs runtime footprint, onboarding friction,
and reviewer attention — the three things this project treats as product
features.

This list is about the CLI process model, not about the deployed platform's
security. Authentication, credential scope, and stage data isolation in the
running stack are real requirements — open items are tracked as debt in
`docs/technical-debt.md`, not waived here.

## Orientation — read in this order

1. `README.md` — stack, deployment targets, local workflow
2. [GitHub milestones](https://github.com/OpenLakeForge/openlakeforge/milestones) — release scope and exit gates; the
   roadmap plan issue (#183) sequences them
3. `docs/architecture/overview.md` and `docs/architecture/provider-contracts.md`
4. `docs/adr/README.md` — the decision log index; each ADR describes what
   binds today, all worth reading
5. `docs/technical-debt.md` — the live debt register with a fix path per item
6. `docs/architecture/diagrams/README.md` — pod census and runtime topology

Current work is tracked in GitHub milestones. Issues carry `priority: P0/P1/P2`
labels expressing intended sequence within a milestone.

## Repository map

| Path | Contains |
| --- | --- |
| `lakehouse_code/bronze/<source>/` | Source-owned: `source.yaml` descriptor plus dlt extract |
| `lakehouse_code/silver/<domain>/` | Domain-owned: Floe contracts and transformations |
| `lakehouse_code/gold/<product>/` | Product-owned: dbt project |
| `lakehouse_code/dashboards/superset/<dashboard>/` | Consumption-owned: Superset reports |
| `lakehouse_code/pipelines/dagster/` | User-maintained Dagster orchestration code |
| `lakehouse_code/lakehouse.yaml` | Canonical domain/product business metadata descriptor |
| `openlakeforge.yaml` | Project-root Deployment Profile v1; parsed and resolved by `olf profile validate`/`resolve` (ADR 0011) |
| `openlakeforge.conformance.yaml` | Local DEV+PROD conformance profile (#155); the nightly applies it verbatim (#220) |
| `libs/` | Shared runtime Python imported by the project-code image |
| `packages/domain-model/` | Canonical provider-neutral descriptor and inventory package |
| `tools/olf/` | The `olf` CLI — uv-managed deploy tooling, contracts, artifacts, scaffolding, e2e |
| `infra/terraform/environments/` | Per-environment wiring; `contracts.tf` is the contract surface |
| `infra/terraform/foundations/` | Cluster and registry creation (kind, AKS, EKS) |
| `infra/terraform/modules/` | Component modules grouped by capability |
| `infra/helm/values/local/` | Helm values for the local profile |
| `images/project-code/` | The Dagster runtime image |
| `release/component-catalog.yaml` | Immutable version, digest pins, and the distribution version contract both `pyproject.toml` files must match |
| `docs/schema/lakehouse.schema.json`, `docs/schema/source.schema.json` | Schemas `lakehouse.yaml` and `source.yaml` validate against |
| `docs/schema/deployment-profile.schema.json` | Schema for Deployment Profiles |

## Architectural rules

These are load-bearing. Breaking one means the change is wrong even if it works.

1. **Contracts before providers.** Components communicate through the provider
   contracts in `contracts.tf`, never directly with a provider. Adding a
   capability means extending the contract, then writing an adapter.
2. **Descriptors stay provider-neutral.** `lakehouse_code/lakehouse.yaml` and
   `lakehouse_code/bronze/<source>/source.yaml` must not name Polaris, Glue, S3,
   or SeaweedFS. Physical catalog and object-store names are derived from these
   logical identities by provider/deployment contracts, never embedded in the
   descriptors themselves — see `docs/schema/lakehouse.schema.json` and
   `docs/schema/source.schema.json`.
3. **Three deploy phases, two lifecycles** (ADR 0002). `olf deploy` runs
   `foundation` → `platform` → `artifacts` in order, and each is selectable
   with `--phase`. Foundation and platform are static infrastructure that
   Terraform owns; artifacts is everything derived from `lakehouse_code/`.
   That static/dynamic split is the CD boundary — a code commit runs artifacts
   and never invokes Terraform. **Never make a platform apply wait on an
   artifact:** anything needing user code belongs in artifacts.
4. **`olf` owns repository orchestration** (ADR 0008). Cross-environment
   logic lives in `tools/olf` with tests. There is no tracked shell; `olf
   check structure` rejects it. `olf` is the only orchestration surface —
   there is no `Makefile`.
5. **No hardcoded product knowledge in shared code.** Product lists, job names,
   table names, and dashboard names are moving to descriptor-driven discovery.
   Do not add new ones.
6. **Pins are immutable.** Images are pinned by digest and recorded in
   `release/component-catalog.yaml`. Changing a version means updating the
   catalog in the same change; `olf check components` enforces this.
7. **No credentials in generated artifacts or runtime defaults.** Floe manifests
   are generated by Floe and never post-processed. Secrets reach pods through
   Kubernetes Secret references.
8. **An ADR describes what binds today.** Anything that moves a component
   boundary, replaces a technology, or changes the deploy model must be
   reflected in `docs/adr/`. When a decision changes, **rewrite the ADR that
   owns it** and note what changed in its History footer — do not stack a new
   ADR that supersedes an old one and leave both to be read. Add a new
   numbered ADR only for a genuinely new decision area, with an entry in
   `docs/adr/README.md`. If an ADR contradicts the code, one of the two is a
   bug; say which in the pull request.

## Writing code here

Three failure modes show up repeatedly in generated changes. Each costs more to
review than it saved to write.

**Comments carry the why, never the what.** A comment earns its place by
recording something the reader cannot recover from the code: a provider
constraint, an upstream bug, a trade-off, a reason the obvious approach fails.
Two examples of the house pattern —
`tools/olf/olf/deployment/cloud/config.py` explaining why `var_file` and
`foundation_var_file` are separate channels (because a shared one would fail
Azure's `-var-file` validation), and `tools/olf/tests/conftest.py`'s toolchain
fixture explaining that without it, tests download real binaries into the
developer's home directory.

Do not narrate. No `# Step 1: ...` sequences, no docstring on a three-line
helper whose name already says it, no comment restating the line beneath it.
Deleting a comment should lose information; if it does not, it should not have
been written.

**Tests assert behaviour, not implementation.** A correct rewrite of the
implementation must keep the test passing. Do not assert on private attributes
or internal call order, and do not write a mock that restates the function body
— that tests the mock. Drive behaviour from fixtures, as
`tools/olf/tests/test_contracts_check.py` does with its invalid-descriptor
files. One module per `olf` module, broadly; new `olf` behaviour ships with
tests in the same change.

**Every claim in the docs is verified before it ships.** Run the command, or
grep the path, or do not write the sentence. This is not hypothetical: four
places in this repository documented `olf e2e run --suite smoke` as *the*
end-to-end validation while `e2e/_runner.py` only launches
`inventory.default_product`, so the advertised check could pass with another
product's pipeline broken. When a doc names a module, flag, path, or default,
check it against the tree in the same change.

No marketing adjectives, and no restating a heading as its own first sentence.

## One change per pull request

A pull request should be reviewable in one sitting. **Over roughly 15 files or
600 changed lines, split it** — and say in the body why the remainder is a
separate change.

This is a hard-won rule, not style. The v0.2/v0.3 milestones landed as pull
requests of 104, 120, and 89 files; review findings per change rose with them,
because a diff that size gets skimmed rather than read, by humans and review
bots alike. The gates below all pass on a 4,000-line change, so nothing except
this rule stops one.

Split along the seams the architecture already has:

1. The contract change (`contracts.tf`, `provider_contracts.py`, a schema) with
   its tests, and no adapter using it yet.
2. One pull request per provider adapter — local, then AWS, then Azure.
3. Docs and ADR updates land with the change that makes them true, not after.

A vertical slice through one provider is a better first pull request than a
horizontal one across all three: it proves the contract against a real caller
before two more adapters are written to the same assumptions.

## Standards

**Python.** 3.12 — `project-code` requires `>=3.12,<3.13`, `tools/olf` requires
`>=3.12`. Ruff with line length 120 and rules `E,F,W,I,UP,B`; configuration in
`tools/olf/pyproject.toml`. Follow the surrounding style: `from __future__ import
annotations` at module top, type annotations on public functions, frozen
dataclasses for value objects.

**Tests.** pytest under `tools/olf/tests/`. See "Writing code here" above.

**Terraform.** Modules follow `main.tf` / `variables.tf` / `outputs.tf`;
`olf check structure` requires all three for registered Terraform modules.
Variables carry a `description` and an explicit `type`.

**Docs.** Hard-wrap around 80 columns, as the existing files do. Prefer tables
over prose for anything enumerable. No plain box-and-arrow Mermaid flowcharts —
the architecture charts use the Kubernetes-icon SVG toolkit in
`docs/architecture/diagrams/src/`; sequence diagrams are fine.

**Commits.** `type: subject`, imperative, lowercase after the colon. Types in
use: `feat`, `fix`, `docs`, `refactor`, `chore`. The body explains why.

**Structure.** `olf check structure` holds the repository skeleton contract and
rejects tracked shell scripts. A new file that should always exist must be
registered there.

## Gates — all must pass before opening a pull request

```bash
uv run --project tools/olf olf check all
uv run --project tools/olf ruff check tools/olf
uv run --project tools/olf mypy --config-file tools/olf/pyproject.toml tools/olf/olf
uv run --project tools/olf pytest tools/olf/tests --cov --cov-config=tools/olf/pyproject.toml
```

Run all four from the repository root, which is what the relative paths above
are written against. Coverage also needs `--cov-config` because it will not
otherwise find its settings — without the flag it silently measures line
coverage only and enforces no threshold at all.

**Type checking** is a ratchet, not a wall. 121 of 147 modules are checked; the
26 that had pre-existing errors are listed under `[[tool.mypy.overrides]]` in
`tools/olf/pyproject.toml`. Removing a module from that list needs no
justification. Adding one needs a reason in the pull request body — and is
almost always the wrong answer to a new type error.

**Coverage** is a floor at 81% branch coverage, for the same reason: a change
may not lower it. Raise the floor when it rises; never lower it to go green.

`olf check all` runs the check targets above plus the release-readiness
gate. It runs on every pull request via `.github/workflows/checks.yml`.

`main` is protected by the `main_protection` repository ruleset, so some of
those checks are merge-blocking. Six contexts are required, and pull requests
merge through a **merge queue**: "Merge when ready" adds a pull request to it,
and the queue re-runs the required checks on the pull request merged with
`main` and everything queued ahead of it, then squash-merges. A pull request
therefore no longer needs updating from `main` by hand; `checks.yml` runs on
`merge_group` for that reason.

| Required to merge | Not required |
| --- | --- |
| Validate repository structure | Deploy slim kind smoke |
| Validate infrastructure contracts | Validate release readiness |
| Validate provider contracts | |
| Validate project-code runtime | |
| Validate deployment tooling | |
| Validate product dbt projects | |

The ruleset also **requires every review thread to be resolved**, which is what
usually blocks an otherwise-green pull request: `mergeStateStatus` reads
`BLOCKED` with each check passing. Approvals are not required, but a change
GitHub cannot attribute to its author needs one. Merges are squash or rebase
only, and `main` rejects deletion and force-pushes.

Read the live rules with
`gh api repos/OpenLakeForge/openlakeforge/rules/branches/main`.
The branch-protection endpoint (`.../branches/main/protection`) returns 404
"Branch not protected" because this is a ruleset rather than classic branch
protection — that 404 is not evidence that `main` is unprotected.

When a change touches the deployed stack, verify at runtime as well:

```bash
uv run --project tools/olf olf deploy --provider local
uv run --project tools/olf olf e2e run --env local
```

`olf e2e run --env local` launches every product pipeline, verifies Silver and Gold
tables through Trino, checks Superset dashboards and OpenMetadata assets, and
confirms ops-bucket artifacts exist.

## Guardrails

- Never commit to `main`. Branch, then open a pull request.
- Never edit generated Floe manifests under
  `lakehouse_code/silver/<domain>/contracts/floe/manifests/`. They are produced
  by `olf floe generate-manifests` and owned by Floe.
- Never unpin an image or chart to make something work.
- Never widen scope beyond the issue. Note adjacent problems in the pull request
  body instead of fixing them silently.
- If a gate fails for a reason unrelated to the change, say so in the pull
  request rather than working around it.
- Ask before inventing facts that belong to the maintainer: security contacts,
  ownership, support commitments, or anything that implies a promise to users.

## Known gaps

Governance artifacts — `CONTRIBUTING`, `SECURITY`, `SUPPORT`, `GOVERNANCE`, and
`CODEOWNERS` — do not exist yet, and there is no dependency-update automation.
`main` itself is protected; see "Gates" for what that ruleset enforces.
Tracked in
[#37](https://github.com/OpenLakeForge/openlakeforge/issues/37). Until they land, this
file is the contribution guide.
