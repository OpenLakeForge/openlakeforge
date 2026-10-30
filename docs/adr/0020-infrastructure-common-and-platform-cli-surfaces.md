# ADR 0020: Infrastructure, common and platform CLI surfaces

## Status

Partly binding. **Binding today:** the `olf infra` group, the common top-level
commands, and the hidden aliases that keep every earlier path working (#536,
PR 2b), and the platform session commands `olf login`, `whoami` and `logout`
(PR 4). **Decided, not built:** the remaining platform commands (`domain list`,
`query`) and the non-interactive authentication contract below. Platform
commands are clients of the platform API, decided in
[ADR 0021](0021-platform-api.md) and not built either.

## Context

`olf` deployed infrastructure and offered end-user operations through one
command tree, and its code did not keep them apart. Every command that reached
a service did so with a kubeconfig, and the service saw a bot or a workload,
never the person. [ADR 0014](0014-identity-and-authorization.md) classified
every `olf` call as class 2, which is right for deployment and leaves no safe
way to hand a person, or their local agent, a CLI.

## Decision

`olf` stays one executable with three surfaces. **The credential a command
needs decides its surface**, not who runs it.

| | Infra (`olf infra ...`) | Common (`olf ...`) | Platform (`olf ...`) |
| --- | --- | --- | --- |
| Needs | Cluster or cloud permissions | None | A Dex session, the same path as the UI |
| Identity | Cloud IAM or SDK identity, kubeconfig and Kubernetes RBAC; workload or CI credentials | None | Human OIDC session; delegated user identity for local agents |
| Reaches | Cloud and Kubernetes APIs, Terraform state, Helm, registry, object store | Local files, local Docker, public release metadata | The platform API ([ADR 0021](0021-platform-api.md)), and Trino's `query_api` route for data-plane reads; never Kubernetes Secrets, exec or port-forward |

Being `platform-admin` in the OpenLakeForge UI grants no Kubernetes or cloud
rights. A platform command cannot reach a privileged operation through an infra
client, a shared service-admin token or an implicit port-forward.

Code follows the same split: `olf/common` imports neither `olf/infra` nor
`olf/platform`, and `tools/olf/tests/common/test_package_boundaries.py` fails
on a violation. `olf/cli.py` is the only module that imports both. When
`olf/platform` exists it imports nothing from `olf/infra`, and the same test
gains that rule. The service operations both surfaces need live in a library
the infra commands and the platform API share (ADR 0021, decision 4), not in
either command package.

## Command ownership

| Surface | Commands |
| --- | --- |
| Infra | `deploy`, `plan`, `destroy`, `doctor`, `status`, `forward`, `break-glass`, `query`, `auth *`, `platform *`, `access trust`, `github connect`, `secrets rotate`, `k8s`, `catalog`, `artifacts`, `openmetadata *`, `superset *`, `report *`, `project build\|deploy\|status\|image\|revision *`, `product run`, `images load`, `contracts env`, `diagnostics collect`, `dbt parse`, `e2e`, `smoke` |
| Common | `init`, `version`, `check *`, `release *`, `toolchain *`, `distribution *`, `profile validate\|resolve`, `source new`, `domain new`, `product new`, `project validate\|graph`, `contracts check`, `images build`, `layers enabled`, `diagnostics tree` |
| Platform | `login`, `whoami`, `logout`; `domain list` and `query` are planned |

Groups that mix credentials exist on both sides: `olf project validate` is
common and `olf infra project deploy` is infra.

Two commands sit in infra although #536 lists them as common, because their
code is not permission-free yet:

- `project image` builds through the cloud provider and needs cloud credentials
  on `aws` and `azure`.
- `report validate` calls the bundle validators in `olf.infra.superset`.

They move to common when those validators move.

`olf infra query` is workload-scoped: it runs as the stage's shared Trino
principal. Per-person access is [ADR 0019](0019-data-plane-authorization-deferral.md).
When that path is built, `olf query` becomes the platform command and
`olf infra query` keeps the workload path for operators. The hidden alias
`olf query` → `olf infra query` ends in that change, the one exception to the
migration rule below, and `test_cli_surfaces.py` drops it in the same change.

`olf check contracts` and `olf contracts check` are common, but one of their
checks (`hcl_phase_two_invariants`) runs `terraform output` against applied
state. It is skipped when no environment is applied; with an applied remote
backend and cloud credentials in the environment it reads that state.

## Migration

Every earlier path stays as a hidden alias of its new home: it runs the same
callback, takes the same options, and does not appear in `--help`.
`olf deploy` and `olf infra deploy` are one command; so are `olf project
deploy` and `olf infra project deploy`. Aliases print no warning yet.
Deprecation warnings turn on only after the documentation and CI workflows use
the new paths. `tools/olf/tests/common/test_cli_surfaces.py` holds the list of
earlier paths and fails if one stops resolving to its new home.

## Non-interactive platform authentication

CI and service agents cannot complete a browser login. When they need the
platform surface, for example to publish project artifacts, the credential must:

- be short-lived, valid for minutes and issued per run;
- be scoped to the operation and project it serves, and carry no infra rights;
- identify the workload or pipeline, not a person;
- come from exchanging an identity the runner already holds, not from a secret
  stored in CI;
- never be a human refresh token or personal access token, and never be
  derived from one.

An infra credential is not a substitute. Infrastructure CI/CD keeps using
cloud and Kubernetes permissions. The exchange is not built; it needs its own
design against these requirements. Until it is, the artifacts phase imports
reports and metadata with infra credentials and does not call the platform API
(ADR 0021, decision 4).

## Consequences

- A person or agent can be given the platform surface without a kubeconfig.
- Every old script keeps working until the deprecation window opens.
- Two infra-side commands move later (above).
- The `olf infra` documentation paths land with PR 6 of #536.

## History

New record (#536).

Rewritten for [ADR 0021](0021-platform-api.md): the platform surface reaches
the platform API instead of each service's public route; `olf query` joins the
planned platform commands, and its hidden alias to `olf infra query` ends when
ADR 0019's person path is built.

The platform session commands exist (#536, PR 4): `olf/platform` holds `olf login`,
`olf whoami` and `olf logout`, registered at the top level. Until the platform
API serves its discovery document (ADR 0021), the instance comes from the
Deployment Profile: the issuer is `https://auth.<base_domain>` for
`identity.issuer: github` and `issuer_url` for `external`, and the client is
`identity.client_ids['olf-cli']` or `olf-cli`. The session is a mode-0600 file
under `OLF_HOME`; the OS keyring is a follow-up. A test holds the import
direction: `olf/platform` imports only `olf.common`.
