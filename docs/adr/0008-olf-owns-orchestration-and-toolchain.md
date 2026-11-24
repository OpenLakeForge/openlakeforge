# ADR 0008: `olf` owns orchestration and its managed toolchain

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS ✔ · Azure ✔ |

## Context

Deployment automation was once about 6,900 lines of shell across
`scripts/local`, `scripts/azure` and `scripts/aws`. Environment-agnostic logic
was copied three times, and contract resolution, OpenMetadata seeding,
Superset bundles, object-storage uploads and image bookkeeping lived as Python
heredocs inside bash. Splitting shell (orchestration) from Python (logic) left
two runtime paths and let a distribution depend on checkout-local scripts.
Separately, a user had to install Terraform, Helm, kubectl and kind globally,
at compatible versions, before a deploy would run: OpenLakeForge's
implementation details were the user's prerequisites.

## Decision

**D1. `olf` is the only orchestration implementation.** `tools/olf` owns
deployment sequencing, artifact preparation, validation, diagnostics and
release helpers. It invokes Terraform, Helm, kubectl, Docker and kind as
managed external processes with structured argv, retries and diagnostics.
Terraform stays the state and drift engine and Helm the chart and release
engine; `olf` sequences them and reimplements neither. Enforced by: the typed
adapters in `tools/olf/olf/common/tooling/` and
`tools/olf/olf/infra/tooling/`.

**D2. The repository tracks no shell and no `Makefile`.** `olf` is the only
orchestration surface. The exceptions are the `*.sh.tftpl` templates
Terraform renders into in-cluster Jobs, kept as shell because a release in
the same apply reads their output at start, or because only that container
image can run the step; each is listed with its reason. Enforced by: `olf
check structure` and `SHELL_TEMPLATES` in
`tools/olf/olf/common/commands/checks/_structure.py`, which fail on an
unlisted template or a stale entry.

**D3. Every provider runs one lifecycle substrate.** A `DeploymentEngine`
sequences a `DeploymentProvider`. The cloud providers share one
`CloudProvider`, and a `CloudBackend` protocol isolates the real AWS and
Azure differences (foundation variables, kubeconfig population, registry
login, default image repository, Floe profile selection) rather than forking
the lifecycle. A new deployment step goes into a provider implementation with
tests, never into a script. Enforced by:
`tools/olf/olf/infra/deployment/engine.py`,
`tools/olf/olf/infra/deployment/cloud/backend.py` and
`tools/olf/tests/infra/test_provider_conformance.py`.

**D4. `olf` provisions its own toolchain.** It downloads, checksum-verifies
and privately invokes Terraform, Helm, kubectl and kind under `OLF_HOME`
(default `~/.openlakeforge`), at the exact versions
`release/component-catalog.yaml` pins: a pin the user's `PATH` can override is
a pin in name only. `OLF_TOOLCHAIN_MODE=host` resolves them from `PATH`
instead, for users who supply their own binaries. Enforced by:
`tools/olf/olf/common/tooling/resolver.py`.

**D5. Cloud authentication goes through the vendor SDKs.** AWS IAM Identity
Center and Microsoft Entra sign-in run through boto3 and the Azure SDKs;
neither the `aws` nor the `az` CLI is a prerequisite. `olf infra auth login`
opens the vendor-hosted sign-in page, and OpenLakeForge never collects or
hosts credentials. Enforced by: `tools/olf/olf/infra/auth/`.

**D6. Exactly two credential bridges stand in for the vendor CLIs.** EKS
kubeconfigs get `olf`'s own credential process instead of an `exec` plugin
that calls `aws`, and the pinned AzureRM provider gets a minimal `az` bridge
for the account metadata and tokens it asks for under interactive user auth.
These are the seams where D5's claim does real work: changing either changes
that claim. Enforced by: `tools/olf/olf/infra/aws_credential_process.py`,
`tools/olf/olf/infra/azure_bridge.py` and
`tools/olf/tests/infra/test_auth.py`.

## Consequences

- A clean machine needs Docker and Python 3.12; `olf` provides everything
  else.
- New `olf` behaviour ships with tests in the same change.
- CI caches the managed toolchain by the hash of
  `release/component-catalog.yaml`, so a new pin invalidates exactly that
  cache entry.
- The `*.sh.tftpl` templates stay shell: porting one to an `olf` step after
  the apply would make the release that reads it wait on `olf`.

## Boundaries

| Not decided here | Where |
| --- | --- |
| The deploy phases, their order and the CD boundary | [ADR 0002](0002-deployment-lifecycle.md) |
| Which surface a command sits on and the command list | [ADR 0020](0020-infrastructure-common-and-platform-cli-surfaces.md); [CLI reference](../reference/cli.md) |
| What `OLF_HOME` holds besides the toolchain (the platform payload) and the published distributions | [ADR 0009](0009-distribution.md) |
| Which tool versions are pinned | `release/component-catalog.yaml`; [component catalog](../reference/component-catalog.md) |
| AWS and Azure foundations and their maturity | [ADR 0010](0010-cloud-provider-implementations.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Shell orchestrates, Python owns cross-environment logic | Two runtime paths; a distribution still depends on checkout-local shell |
| A `Makefile` of one-line `olf` delegates (#212) | A duplicate surface that drifted despite its tests |
| Host-installed Terraform, Helm, kubectl and kind | Pins become advisory; the user inherits implementation details |
| The `aws` and `az` CLIs as prerequisites | Extra installs for two narrow needs D6 covers |

## History

- 2026-09-17 (#212): D2: the `Makefile` was deleted; `olf` is the only
  orchestration surface.
- 2026-11-06 (#562): D2: the OpenMetadata and Polaris bootstraps left their
  Terraform shell templates for `olf`; the remaining templates are listed in
  `SHELL_TEMPLATES`.
- 2026-11-22: Rewritten in the standard form with D1 to D6; no decision
  changed.

Merges the earlier records 0028 (Python owns repository orchestration), 0029
(the managed toolchain) and 0030 (SDK-managed cloud authentication).
