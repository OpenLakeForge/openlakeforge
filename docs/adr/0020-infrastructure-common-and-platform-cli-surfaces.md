# ADR 0020: Infrastructure, common and platform CLI surfaces

| | |
| --- | --- |
| Status | Binding |
| Built | D1 to D5 ✔ · D6 ✘ (no non-interactive platform credential exchange) |

## Context

`olf` deployed infrastructure and offered end-user operations through one
command tree, and its code did not keep them apart. Every command that
reached a service did so with a kubeconfig, and the service saw a bot or a
workload, never the person. [ADR 0014](0014-identity-and-authorization.md)
classified every `olf` call as class 2, which is right for deployment and
leaves no safe way to hand a person, or their local agent, a CLI.

## Decision

**D1. `olf` is one executable with three surfaces, and the credential a
command needs decides its surface, not who runs it.**

| | Infra (`olf infra ...`) | Common (`olf ...`) | Platform (`olf ...`) |
| --- | --- | --- | --- |
| Needs | Cluster or cloud permissions | None | A Dex session, the same path as the UI |
| Identity | Cloud IAM or SDK identity, kubeconfig and Kubernetes RBAC; workload or CI credentials | None | Human OIDC session; delegated user identity for local agents |
| Reaches | Cloud and Kubernetes APIs, Terraform state, Helm, registry, object store | Local files, local Docker, public release metadata | The platform API ([ADR 0021](0021-platform-api.md)), and directly the public route of a service that authorizes the person's token, as Trino's `query_api` for `olf query` (ADR 0021, decision 2); never Kubernetes Secrets, exec or port-forward |

Enforced by: `tools/olf/tests/common/test_common_without_credentials.py`.

**D2. Being `platform-admin` in the OpenLakeForge UI grants no Kubernetes or
cloud rights.** A platform command cannot reach a privileged operation
through an infra client, a shared service-admin token or an implicit
port-forward. Enforced by: D3's import rule, which keeps infra clients out of
`olf/platform`.

**D3. The code follows the surfaces.** `olf/common` imports neither
`olf/infra` nor `olf/platform`; `olf/platform` imports nothing from
`olf/infra`; `olf/cli.py` is the only module that imports both. Service
operations both surfaces need live in the library the infra commands and the
platform API share (ADR 0021, decision 4), not in either command package. Enforced
by: `tools/olf/tests/common/test_package_boundaries.py`.

**D4. Surface is decided per command, not per group.** A group may span
surfaces: `olf project validate` is common and `olf infra project deploy` is
infra. `olf infra project image` is infra because it builds through the
cloud provider and needs cloud credentials on `aws` and `azure`. One
exception is accepted: `olf check contracts` and `olf contracts check` are
common, but their `hcl_phase_two_invariants` check runs `terraform output`
against applied state; it is skipped when no environment is applied and reads
remote state only when cloud credentials are already in the environment. The
command list is in the [CLI reference](../reference/cli.md#commands-by-surface).
Enforced by: the group registration in `tools/olf/olf/cli.py`.

**D5. Every earlier command path stays as a hidden alias of its new home,
except the ones that became platform commands.** An alias runs the same
callback with the same options, is absent from `--help`, prints one line on
stderr naming the new path and that the old one will be removed in a later
release, and then behaves as before, with the same stdout and exit code. A
command that moved from infra to common keeps its `olf infra` path the same
way (`olf infra report validate` runs `olf report validate`). The exceptions
are `olf query`, a person's own queries ([ADR 0019](0019-data-plane-authorization-deferral.md)),
and `olf report import|export`, one report as the signed-in person through
the platform API: they are platform commands, not aliases, while `olf infra
query` and `olf infra report import|export` stay for operators. Enforced by:
`tools/olf/tests/common/test_cli_surfaces.py`.

**D6. A non-interactive platform credential is short-lived, scoped, and
never a person's.** When CI or a service agent needs the platform surface,
for example to publish project artifacts, the credential must be valid for
minutes and issued per run; be scoped to the operation and project, with no
infra rights; identify the workload or pipeline, not a person; come from
exchanging an identity the runner already holds, not from a secret stored in
CI; and never be, or derive from, a human refresh token or personal access
token. An infra credential is no substitute. Not built: the exchange needs
its own design against these requirements, and until then the artifacts
phase imports reports and metadata with infra credentials and does not call
the platform API (ADR 0021, decision 4).

## Consequences

- A person or agent can be given the platform surface without a kubeconfig.
- Every old script keeps working until the aliases are removed.
- Infrastructure CI/CD keeps cloud and Kubernetes permissions; it cannot use
  the platform surface until D6 is built.

## Boundaries

| Not decided here | Where |
| --- | --- |
| What a deploy does, its phases and the CD boundary | [ADR 0002](0002-deployment-lifecycle.md) |
| How `olf` runs its tools, its managed toolchain, cloud SDK authentication and the no-shell rule | [ADR 0008](0008-olf-owns-orchestration-and-toolchain.md) |
| Every command, its options, and which surface it is on | [CLI reference](../reference/cli.md) |
| The platform API's routes, authorization and audit | [ADR 0021](0021-platform-api.md) |
| What a person may read and write through `olf query` | [ADR 0019](0019-data-plane-authorization-deferral.md) |
| Identity classes and canonical roles | [ADR 0014](0014-identity-and-authorization.md); [identity classes](../architecture/identity/identity-classes.md) |
| The platform session: issuer, client id, session file | [CLI reference](../reference/cli.md#sign-in-from-the-cli) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| One command tree where every command uses a kubeconfig | The service never sees the person; no safe CLI for a person or agent |
| Infra credentials in CI for platform operations | Gives CI more than the operation needs (D6) |

## History

- 2026-10-30 (#536): New record: D1 to D6.
- 2026-10-30 (#652): D1's platform row reaches the platform API instead of
  each service's public route.
- 2026-10-30 (#665): D1's platform row may call a service's public route
  directly when the service authorizes the person's token.
- 2026-10-30 (#656): D5's aliases print a warning.
- 2026-10-30 (#694): D5's first exception: `olf query` is a platform command.
- 2026-10-30 (#664): D5 covers a command that moved from infra to common.
- 2026-10-31 (#670): D5's second exception: `olf report import|export`.
- 2026-11-22 (#1385): Corrected: the platform API and `olf/platform` exist.
- 2026-11-22: Rewritten in the standard form with D1 to D6; the command table
  moved to the CLI reference; no decision changed.
