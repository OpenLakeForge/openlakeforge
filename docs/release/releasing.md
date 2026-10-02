# Releasing OpenLakeForge

A tagged OpenLakeForge release must be independently verifiable and
installable from a clean checkout. This document covers both sides of that
contract: how a maintainer cuts a release, and how a consumer verifies one.

See also [component-catalog.md](component-catalog.md) for how the input
inventory is maintained, and [compatibility-matrix.md](compatibility-matrix.md)
for the published compatibility matrix.

## Pipeline shape

`.github/workflows/release.yml` runs on two triggers:

| Trigger | Behavior |
| --- | --- |
| `push: tags: ['v*']` | Full publish: builds, pushes, signs, attests, and creates the GitHub Release. |
| `workflow_dispatch` with `dry_run: true` (default) | Rehearses the entire pipeline without pushing images, creating the tag, or publishing the Release. Every would-be asset (manifest, compatibility matrix, SBOMs, checksums) is uploaded as a workflow artifact instead. |
| `workflow_dispatch` with `dry_run: false` | Allowed **only when the selected ref is a tag**; the workflow fails fast otherwise. Keyless cosign derives its certificate identity from the running ref, so publishing from a branch would stamp `release.yml@refs/heads/<branch>` while consumers verify `release.yml@refs/tags/<tag>` — producing a release that fails its own documented signature verification. Prefer pushing the tag. |

Jobs:

1. **`prepare`** — resolves the release version and tag from
   `release/component-catalog.yaml`'s `distribution.version`, runs
   `olf release check` (the release-readiness gate — see below), and (for
   real, non-dry-run publishes) verifies no GitHub Release already exists
   for that tag. Release tags are **create-only** (`release_tag_policy:
   immutable-semver`): a tag is never republished once its Release exists.
2. **`build`** (matrix: `project-code`, `superset`) — builds each image with
   Buildx, pushes it **by digest** (`push-by-digest=true`) and then tags the
   pushed digest with the release version via `docker buildx imagetools
   create`, so the published reference is always traceable back to an
   immutable digest rather than a movable tag. Generates an SPDX SBOM with
   `anchore/sbom-action` (syft), signs the digest keylessly with
   `cosign sign` (Sigstore OIDC — no long-lived keys), attaches the SBOM as
   a cosign attestation (`cosign attest --type spdxjson`), and attaches SLSA
   build provenance with `actions/attest-build-provenance`.
3. **`bundle`** — assembles the release bundle: the component manifest
   (`olf release manifest`, catalog + resolved image digests + git SHA), the
   compatibility matrix (`olf release compatibility-matrix`), a verbatim
   copy of `release/component-catalog.yaml`, `CHANGELOG.md`, both SBOMs, and
   a deterministic `checksums.txt` (`olf release checksums`) covering every
   asset. On a real publish, keyless cosign signs that checksum manifest and
   publishes its `checksums.txt.bundle` verification bundle alongside it; in
   dry-run mode the unsigned inspection bundle is uploaded as a single
   workflow artifact.

Release orchestration (bundle construction, checksums, compatibility-matrix
rendering, and verification) lives behind `olf release`. Terraform and Helm
remain their respective deployment engines; no release shell wrappers remain.
See [ADR 0008](../adr/0008-olf-owns-orchestration-and-toolchain.md).

## Cutting a release

0. **Before the first tagged release only**: register a PyPI [pending
   trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   for each of `openlakeforge-domain-model` and `openlakeforge`, pointed at
   this repo's `release.yml`. Each must use a **distinct GitHub Environment
   name** (`pypi-domain-model` and `pypi-openlakeforge`, matching the
   `environment:` key on each `publish-pypi-*` job) — PyPI keys a pending
   publisher by `(owner, repo, workflow, environment)`, and both jobs
   sharing this repo and workflow file means an identical, un-scoped
   environment would collide across the two project names.
1. Update `release/component-catalog.yaml`'s `distribution.version` (and any
   changed component versions/digests/lockfiles) in the same PR as the code
   it describes. Run `uv run --project tools/olf --locked olf check all` locally.
2. Add a `## [<version>]` entry to `CHANGELOG.md` with migration notes,
   replacing "Unreleased" with the release date.
3. Merge to `main`. Confirm the `release-check` job in
   `.github/workflows/checks.yml` is green on `main`.
4. **Rehearse first**: trigger `.github/workflows/release.yml` via
   `workflow_dispatch` with `dry_run: true` (the default). Inspect the
   uploaded `release-dry-run-bundle-<version>` artifact — it should contain
   `component-manifest.json`, `compatibility-matrix.md`,
   `component-catalog.yaml`, `CHANGELOG.md`, both SBOMs, and
   `checksums.txt`.
5. Once the dry run looks right, cut the real tag:
   `git tag v<version> && git push origin v<version>`. The tag push triggers
   the same workflow with `dry_run: false`, which builds, pushes, signs,
   attests, and publishes the GitHub Release.
6. Run `uv run --project tools/olf --locked olf release verify-install --tag
   v<version>` (see below) to confirm the published release is independently
   verifiable and installs from a clean checkout.

`olf check all` fails a release if:

- `distribution.version` isn't a valid alpha semver, or (when `--tag` is
  passed) the tag doesn't match it.
- Any catalog image under `components.images` is missing an `@sha256:...`
  digest, or (for images with a Helm deployment source) doesn't match the
  digest actually referenced under `infra/helm/values`.
- Any `.github/workflows/*.yml` `uses:` line isn't pinned to a 40-character
  commit SHA, differs from `components.actions`, or leaves an unused catalog
  action entry behind.
- Any `images/*/Dockerfile` has an unpinned `FROM`/`ARG *_IMAGE=` outside of
  `FROM ${...}` build-arg indirection.
- Any Terraform root's `terraform.required_version` differs from
  `components.terraform.required_version` in the catalog.
- `docs/release/compatibility-matrix.md` doesn't match a fresh render of the
  catalog.
- Any Python lockfile declared in `components.python` is missing, or is out
  of sync with its sibling `pyproject.toml` -- checked with `uv lock --check`
  and a seeded `uv pip compile` re-run (`olf check lockfiles`), not a
  hand-rolled parser.

## Consumer verification

Every release publishes:

- Two signed container images: `ghcr.io/openlakeforge/openlakeforge/project-code`
  and `ghcr.io/openlakeforge/openlakeforge/superset`, tagged with the release
  version and referenced by digest.
- `component-manifest.json` — the component catalog plus the resolved image
  digests and the git commit the release was built from.
- `compatibility-matrix.md` — OpenLakeForge ↔ Kubernetes ↔ Terraform ↔ Helm
  chart ↔ cloud service compatibility, sourced from the catalog.
- `component-catalog.yaml` — the exact input manifest, copied verbatim.
- `project-code.spdx.json` / `superset.spdx.json` — SPDX SBOMs, both
  attached to the Release and available as cosign attestations on the image
  digests.
- `checksums.txt` — sha256 over every asset above.
- `checksums.txt.bundle` — the keyless Sigstore verification bundle that
  authenticates `checksums.txt` before it is trusted.
- `CHANGELOG.md`.

Releases up to and including `v0.2.0-alpha.1` were published before the
repository moved to the `OpenLakeForge` organization. They are signed by the
`malon64/openlakeforge` workflow identity and their images live under
`ghcr.io/malon64/openlakeforge/`. Verify those with that identity and image
path in the commands below, and pass `--repo malon64/openlakeforge` to
`olf release verify-install`.

### Verify signatures

```sh
cosign verify \
  --certificate-identity-regexp '^https://github\.com/OpenLakeForge/openlakeforge/\.github/workflows/release\.yml@refs/tags/v0\.1\.0-alpha\.1$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/openlakeforge/openlakeforge/project-code@sha256:<digest>
```

Repeat for the `superset` image. The digest comes from
`component-manifest.json`'s `resolved_images`, so verification never trusts
a mutable tag.

### Verify the SBOM attestation

```sh
cosign verify-attestation \
  --type spdxjson \
  --certificate-identity-regexp '^https://github\.com/OpenLakeForge/openlakeforge/\.github/workflows/release\.yml@refs/tags/v0\.1\.0-alpha\.1$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/openlakeforge/openlakeforge/project-code@sha256:<digest>
```

### Verify checksums

```sh
cosign verify-blob checksums.txt \
  --bundle checksums.txt.bundle \
  --certificate-identity-regexp '^https://github\.com/OpenLakeForge/openlakeforge/\.github/workflows/release\.yml@refs/tags/v0\.1\.0-alpha\.1$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
sha256sum -c checksums.txt
```

### Verify a clean-checkout install

```sh
uv run --project tools/olf --locked olf release verify-install \
  --tag v0.1.0-alpha.1
```

The command downloads the release assets with `gh release download`, uses the
keyless Sigstore bundle to authenticate `checksums.txt` before checking the
assets, verifies the cosign signature on both published image digests, then
does a fresh `git clone --branch v0.1.0-alpha.1` into a scratch directory and
runs `olf check structure` and `olf check components` from that clean checkout
— proving the tagged tree is self-consistent with no local state carried over.
Pass `--pull-images` to additionally `docker pull` both published images by
digest.

This script requires `git`, `gh`, `cosign`, `uv`, `docker` (only for
`--pull-images`), and either `sha256sum` or `shasum`. It is safe to run by
anyone: it only reads public release assets and verifies public Sigstore
signatures.

## Release evidence

This section records what actually ran for the releases listed below, so the
pipeline described above is documented as exercised rather than intended.
Evidence for `v0.2.0-alpha.1` was not recorded when it was published.

Because the tag-triggered run is the one that builds, pushes, signs, and attests
the images, a failure in that stage would have failed the run; a successful
conclusion is the evidence for image publication and signing. To confirm
independently, run the consumer verification commands in the section above, or
`uv run --project tools/olf --locked olf release verify-install --tag "$TAG"
--pull-images` with `TAG` set to the release tag.

Verification that is still outstanding, and should be recorded here when it
runs:

- An end-to-end execution of `olf release verify-install` against a published
  tag by someone other than the release author.

### Per-release evidence

#### `v0.3.0-alpha.1` (2026-10-01)

| Item | Evidence |
| --- | --- |
| Dry run | Run `36884732828`, `workflow_dispatch` on `main` with `dry_run: true`, concluded `success` before the tag was pushed |
| Release workflow | Run `36885204867`, `event=push`, `ref=v0.3.0-alpha.1` (commit `c475bcc`). Attempt 1 built, signed and attested both images but failed the two PyPI jobs with `invalid-publisher`: the trusted publishers were still registered for `malon64/openlakeforge` after the move to the `OpenLakeForge` organization. After re-registering them for `OpenLakeForge/openlakeforge` (environments `pypi-domain-model` and `pypi-openlakeforge`), attempt 2 concluded `success` |
| Published release | `v0.3.0-alpha.1`, not a draft, marked pre-release |
| PyPI | `openlakeforge` and `openlakeforge-domain-model` `0.3.0a1`, wheel and sdist each |
| Release assets | `CHANGELOG.md`, `checksums.txt`, `checksums.txt.bundle`, `compatibility-matrix.md`, `component-catalog.yaml`, `component-manifest.json`, `project-code.spdx.json`, `superset.spdx.json`, and both packages' wheels and sdists |
| Static gates | Required checks on every pull request and in the merge queue (`merge_group`) |
| Runtime gates | The multi-stage nightly, from an `olf init` project on the built wheel, and a manual DEV+PROD review on `main` (#111) |

A second `workflow_dispatch` with `dry_run: false` on the tag (run
`36889916517`) correctly stopped at the create-only check, because the release
already existed.

#### `v0.1.0-alpha.1` (2026-08-10)

| Item | Evidence |
| --- | --- |
| Release workflow | Run `31401253176`, `event=push`, `ref=v0.1.0-alpha.1`, concluded `success` on 2026-08-10 |
| Published release | `v0.1.0-alpha.1`, not a draft, marked pre-release, published 2026-08-10 |
| Release assets | `CHANGELOG.md`, `checksums.txt`, `checksums.txt.bundle`, `compatibility-matrix.md`, `component-catalog.yaml`, `component-manifest.json`, `project-code.spdx.json`, `superset.spdx.json` |
| Static gates | `olf check all` ran as a job on every pull request; `main` was not yet protected (#37) |

## Lifecycle stages

| Stage | Intended use | Compatibility commitment |
| --- | --- | --- |
| Alpha | Development and product-contract validation; `v0.1.0-alpha.1` is validated on local kind only | Breaking changes allowed with migration notes |
| Beta | Controlled AWS evaluation | Best-effort forward migration within the beta line |
| Release candidate | Operational and upgrade qualification | No planned breaking changes before the associated stable release |
| Stable | Supported production use within the published reference envelope | Compatible changes in minor releases; breaking changes only in major releases |
| Deprecated | Still functional but scheduled for removal | Removal release and migration path published |
| Unsupported | Outside the maintained version window | No fixes or compatibility guarantees |

The exact stable support window should be declared before `v1.0`; until then,
only the latest pre-release is maintained.
