# ADR 0009: Distribution — a PyPI package carrying the platform payload

## Status

Binding.

## Context

The deployment engine was only useful from a repository checkout: Terraform
roots, Helm values, image build assets, schemas, and the demo project were all
resolved relative to the checkout. A published release was not installable — a
user who wanted to run OpenLakeForge cloned it, which is a contributor workflow
being handed to consumers.

That also confused two things a checkout keeps in one place: the platform (which
the user should never edit) and the project (which is entirely theirs).

## Decision

### Two published distributions

`openlakeforge` owns the `olf` console command and pins
`openlakeforge-domain-model` exactly. `release/component-catalog.yaml` is the
canonical release identity; its alpha form (`0.2.0-alpha.1`) maps to PEP 440
(`0.2.0a1`), and the release-readiness gate fails if the catalog and either
`pyproject.toml` disagree.

Publication uses GitHub OIDC Trusted Publishing. GitHub Releases keep signed
checksums, SBOMs, and build provenance.

### The wheel carries a verified platform payload

The `openlakeforge` wheel and sdist embed a deterministic, allowlisted
`platform.tar.gz` — Terraform roots, Helm values, schemas, Dockerfiles, shared
libraries, the release catalog, and the demo project — plus a per-file manifest.
An installed `olf` verifies that manifest and extracts atomically under
`OLF_HOME/distributions` before use.

Helm charts stay **outside** the wheel. The catalog pins each chart's repository,
exact version, and archive SHA-256; `olf` caches and verifies the archive before
Terraform is allowed to use it. Charts are large, independently versioned, and
already have an upstream distribution channel; what matters is that the digest is
checked, not that the bytes ship in the wheel.

### Platform and project are separate roots

| Root | Holds | Mutability |
| --- | --- | --- |
| `distribution_root` | Terraform, Helm values, schemas, Dockerfiles, `libs/`, release catalog | Immutable, shared between projects |
| `project_root` | `openlakeforge.yaml` and `lakehouse_code/` — descriptors, Floe contracts, dashboards, pipelines | The user's, writable |
| state / work / cache roots | Terraform state and data, generated artifacts, Docker staging, Helm downloads | Derived |

An installed `olf` resolves `project_root` to the **current working directory**.
`OPENLAKEFORGE_PROJECT_ROOT` and `--project-root`/`--repo-root` override it. A
source checkout keeps the checkout as the contributor default; the two modes stay
distinct rather than one emulating the other.

`ProjectSpec` is the typed project/distribution boundary for project
consumers. It supplies canonical paths for the profile, descriptors, Bronze,
Silver, Gold, dashboard, and Dagster directories. `olf project validate
--project PATH` emits a JSON report for the structural layout, the Deployment
Profile, descriptors, inventory, and declared assets. The profile's typed
model and its resolution into one effective topology are ADR 0011; `olf
project validate` parses it, `olf profile resolve` resolves it.

The consequence worth naming: scaffolding validates user descriptors against
schemas that live outside the project, because the schema is platform material
and the descriptor is not.

### `olf init` makes a directory into a project

It verifies the payload, provisions or reuses the pinned toolchain, checks
Docker, and copies the packaged demo `lakehouse_code/` into the current
directory — or writes a transitional skeleton under `--empty` (ADR 0005). It
renders `openlakeforge.yaml` from the requested stages (`--stages`, DEV by
default), preset (`--preset`, Slim by default) and name (the directory's,
normalised to a label value), and validates it with the profile parser before
writing anything. `--profile-only` writes only that profile, into a project
that already has `lakehouse_code/`, and needs neither the toolchain nor Docker.

It stages into a sibling directory and renames atomically, and refuses to
overwrite either existing project path. It never installs Docker or uses Git.
Within the project directory it touches only `openlakeforge.yaml` and
`lakehouse_code/`; the toolchain and payload verification steps write outside
it, to the shared, immutable `OLF_HOME` (ADR 0008) — never to the project
itself, and never requiring the user to know that boundary exists to run
`olf init` successfully.

## Consequences

The supported consumer path is:

```text
mkdir my-lakehouse && cd my-lakehouse
pip install openlakeforge
olf init                       # or: olf init --stages dev,prod
olf platform apply -f openlakeforge.yaml
```

No subsequent command needs `--project-root .`.

Automatic Terraform state migration between releases is out of scope for the
alpha. Upgrading is documented per release, not automated.

## History

Merges the decisions previously recorded as ADR 0031 (the PyPI embedded platform
payload) and 0032 (the installed project root). ADR 0032's transitional-project
rule is recorded in ADR 0005 alongside the descriptor model it waives.
Updated for the v0.3 external project contract: `ProjectSpec` makes the
project/distribution split explicit, and `openlakeforge.yaml` is the required
project-root Deployment Profile, parsed and resolved per ADR 0011.

2026-10-01: `olf init` renders and validates the Deployment Profile from
`--stages`, `--preset` and `--name` instead of copying a fixed one, and
`--profile-only` gives an existing project its profile. The consumer path ends
with `olf platform apply -f`.
