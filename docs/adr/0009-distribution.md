# ADR 0009: Distribution — a PyPI package carrying the platform payload

| | |
| --- | --- |
| Status | Binding |
| Built | ✔ (published to PyPI by `.github/workflows/release.yml`) |

## Context

The deployment engine was once usable only from a repository checkout:
Terraform roots, Helm values, image build assets, schemas and the demo project
were all resolved relative to it, so a user who wanted to run OpenLakeForge
cloned it, a contributor workflow handed to consumers. A checkout also keeps
in one place two things that must stay apart: the platform, which the user
never edits, and the project, which is entirely theirs.

## Decision

**D1. Three distributions are published with exact version pins.**
`openlakeforge` owns the `olf` console command and pins `openlakeforge-core`
and `openlakeforge-lakehouse-services` exactly; the latter holds the service
clients `olf` shares with the platform API (ADR 0021).
`release/component-catalog.yaml` is the canonical release identity: its alpha
form (`0.2.0-alpha.1`) maps to PEP 440 (`0.2.0a1`), and the release-readiness
gate fails if the catalog and any of the three `pyproject.toml` files
disagree. Enforced by: `tools/olf/olf/common/release/_readiness.py`
(`olf check components`).

**D2. Publication uses GitHub OIDC Trusted Publishing.** GitHub Releases keep
signed checksums, SBOMs and build provenance. Enforced by:
`.github/workflows/release.yml`.

**D3. The wheel carries a verified platform payload.** The `openlakeforge`
wheel and sdist embed a deterministic, allowlisted `platform.tar.gz`
(Terraform roots, Helm values, schemas, Dockerfiles, shared libraries, the
release catalog and the demo project) plus a per-file manifest. An installed
`olf` verifies that manifest and extracts the payload atomically under
`OLF_HOME/distributions` before use. Enforced by: `tools/olf/build_backend.py`
and `tools/olf/olf/common/distribution.py`.

**D4. Helm charts stay outside the wheel and are verified by digest.** The
catalog pins each chart's repository, exact version and archive SHA-256; `olf`
caches and verifies the archive before Terraform may use it. Enforced by:
`tools/olf/olf/common/deployment/charts.py`.

**D5. Platform and project are separate roots.** Enforced by:
`tools/olf/olf/common/project.py`.

| Root | Holds | Mutability |
| --- | --- | --- |
| `distribution_root` | Terraform, Helm values, schemas, Dockerfiles, `openlakeforge_runtime/`, release catalog | Immutable, shared between projects |
| `project_root` | `openlakeforge.yaml` and `lakehouse_code/`: descriptors, Floe contracts, dashboards, pipelines | The user's, writable |
| state, work and cache roots | Terraform state and data, generated artifacts, Docker staging, Helm downloads | Derived |

**D6. An installed `olf` resolves `project_root` to the current working
directory.** `OPENLAKEFORGE_PROJECT_ROOT` and `--project-root`/`--repo-root`
override it. A source checkout keeps the checkout as the contributor default;
the two modes stay distinct rather than one emulating the other. Enforced by:
`tools/olf/olf/common/config.py`.

**D7. `ProjectSpec` is the typed boundary between project and distribution.**
It supplies the canonical paths for the profile, descriptors, Bronze, Silver,
Gold, dashboard and Dagster directories. `olf project validate --project PATH`
emits a JSON report for the structural layout, the Deployment Profile,
descriptors, inventory and declared assets. Scaffolding validates user
descriptors against schemas outside the project, because the schema is
platform material and the descriptor is not. The profile's model and its
resolution are ADR 0011. Enforced by: `ProjectSpec` in
`tools/olf/olf/common/project.py`.

**D8. `olf init` makes the current directory into a project.** It verifies the
payload, provisions or reuses the pinned toolchain, checks Docker, and copies
the packaged demo `lakehouse_code/` into the directory, or writes a
transitional skeleton under `--empty` (ADR 0005). It renders
`openlakeforge.yaml` from the requested stages (`--stages`, DEV by default),
preset (`--preset`, Slim by default) and name (the directory's, normalised to
a label value), and validates it with the profile parser before writing
anything. `--profile-only` writes only that profile into a project that
already has `lakehouse_code/`, and needs neither the toolchain nor Docker.
Enforced by: `tools/olf/olf/common/initialization.py`.

**D9. `olf init` never overwrites and touches only the project's two paths.** A
full initialization stages into a sibling directory and renames into place,
and refuses to overwrite either existing project path; `--profile-only`
creates `openlakeforge.yaml` with an exclusive create, refusing an existing
one. It never installs Docker or uses Git. Within the project directory it
touches only `openlakeforge.yaml` and `lakehouse_code/`; toolchain and payload
verification write to the shared, immutable `OLF_HOME` (ADR 0008), never to
the project, and the user need not know that boundary exists. Enforced by:
`tools/olf/olf/common/initialization.py`.

## Consequences

- The supported consumer path is `mkdir my-lakehouse && cd my-lakehouse`,
  `pip install openlakeforge`, `olf init` (or `olf init --stages dev,prod`),
  then `olf infra platform apply -f openlakeforge.yaml`; no later command
  needs `--project-root .`.
- Automatic Terraform state migration between releases is out of scope for
  the alpha: upgrading is documented per release, not automated.

## Boundaries

| Not decided here | Where |
| --- | --- |
| The Deployment Profile's model and resolution | ADR 0011 |
| The pinned toolchain and `OLF_HOME` | ADR 0008 |
| The transitional (`--empty`) project | ADR 0005 |
| Release mechanics and GitHub as the forge | ADR 0018, `docs/release/releasing.md` |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Running from a repository checkout only | A contributor workflow handed to consumers, mixing platform and project in one tree |
| Shipping Helm chart archives inside the wheel | Charts are large, independently versioned and already distributed upstream; what matters is the checked digest (D4) |

## History

- Undated: New record, merging the former ADRs 0031 (the PyPI embedded
  platform payload) and 0032 (the installed project root); 0032's
  transitional-project rule moved to ADR 0005. D7 added for the v0.3 external
  project contract: `ProjectSpec` and `openlakeforge.yaml` as the required
  project-root profile (ADR 0011).
- 2026-10-01: D8 changed: `olf init` renders and validates the profile from
  `--stages`, `--preset` and `--name` instead of copying a fixed one, and
  `--profile-only` gives an existing project its profile.
- Undated: D1 changed: `openlakeforge-lakehouse-services` became the third
  published distribution (ADR 0021).
- 2026-11-22: Rewritten in the standard form; no decision changed.
