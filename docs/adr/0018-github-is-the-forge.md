# ADR 0018: GitHub is the forge — source, CI/CD, packaging and people

## Status

Partly binding. **Binding today:** OpenLakeForge's own source, CI and
release run on GitHub (table below). **Decided, not built:** a client
project's CD as a GitHub Actions workflow, and GitHub organisation teams as
the source of human identity and role assignment through the Dex adapter of
[ADR 0014](0014-identity-and-authorization.md).

## Context

The audience is a small data team without a platform engineer. Such a team
already keeps its repository, its CI runners, its container images and the
list of who works there in one place: its forge. Every system OpenLakeForge
adds beside that one is a second place to onboard and offboard people, to
rotate a credential, and to look when something breaks.

v0.5-alpha first answered "who may use the platform" with a user directory of
its own: Keycloak accounts, email invitations, SMTP, setup links, and `olf
users` to manage them (#331). That works, but it duplicates the membership
the team already maintains in GitHub, and offboarding someone becomes two
actions, one of which is easy to forget.

## Decision

GitHub is the one forge OpenLakeForge targets. A client project is a GitHub
repository, and OpenLakeForge reuses what that repository already has rather
than standing up a parallel system.

| Concern | What OpenLakeForge uses | State |
| --- | --- | --- |
| Source | A GitHub repository holding `openlakeforge.yaml` and `lakehouse_code/` (ADR 0009, project root) | binding |
| CI of this repository | `.github/workflows/checks.yml` on every pull request and `merge_group`; `main` behind the `main_protection` ruleset and a merge queue | binding |
| Release of this repository | `.github/workflows/release.yml`: PyPI through GitHub OIDC Trusted Publishing, GitHub Releases with checksums, SBOMs and provenance (ADR 0009) | binding |
| Project images | GHCR. `local-full-e2e.yml` pushes the nightly `project-code-nightly` image there; `docs/setup/local.md` documents GHCR (or Docker Hub) for promotable revisions | binding for this repository; the client's registry stays an environment setting |
| Client CD | A GitHub Actions workflow running `olf project build` and `olf project deploy` on the CD boundary of ADR 0002 | decided, not built; `olf init` scaffolds no workflow yet |
| People and roles | GitHub organisation membership and teams. Dex brokers GitHub login into the `identity.oidc` contract; each canonical role maps to one or more teams | decided, not built (ADR 0014, seam 2) |
| User administration | GitHub: inviting, removing and changing someone's team is done in GitHub (web, `gh`, or the client's own GitHub Terraform). `olf users` is removed | decided; removal in progress |

### Why Dex rather than GitHub OAuth in every service

GitHub's user login is OAuth 2.0 without OpenID Connect: it issues no ID
token and no groups claim. (GitHub's OIDC tokens exist only for Actions
workloads.) Superset, OpenMetadata, the Trino UI and the perimeter consume
the provider-neutral `identity.oidc` contract of ADR 0014; wiring each of
them to GitHub's API directly would put a provider-specific adapter in every
consumer. Dex's GitHub connector turns a GitHub login into an OIDC token
whose `groups` claim lists the user's teams as `<org>:<team-slug>`, so the
contract, the role mapping and every consumer stay unchanged. Dex keeps no
user database: refresh tokens and signing keys live in Kubernetes objects.

### Boundaries

| Not decided here | Where |
| --- | --- |
| How canonical roles map to teams, revocation latency, break-glass | ADR 0014 and `docs/architecture/identity-sessions-and-recovery.md` |
| Workload credentials (Trino, Polaris, object storage) | ADR 0015, ADR 0016, `identity-callers.md`; GitHub is never a workload credential inside the cluster |
| GitHub Actions OIDC as the credential CI uses to reach the platform | not decided; CI keeps Kubernetes RBAC through kubeconfig (`identity-callers.md`, class 2) |
| Other forges (GitLab, Bitbucket, Azure DevOps) | out of scope. `spec.identity.issuer: external` still accepts any OIDC issuer, and Dex has connectors for GitLab, Google and Microsoft, so another forge is a connector, not a new identity system |

## Consequences

- Inviting, offboarding and changing a role need no OpenLakeForge command and
  no apply: they are GitHub team changes, under the client's existing GitHub
  audit log and 2FA policy.
- `olf users`, the `shared.identity.admin` contract, the Keycloak `olf-users`
  client and `spec.identity.bootstrap_admins` are removed (#383). The first
  administrator is whoever is in the team mapped to `platform-admin`.
- Every platform user needs a GitHub account in the mapped organisation. On a
  paid GitHub plan each member is a seat, which matters for dashboard viewers;
  a separate free organisation used only for platform access is an option.
- Signing out of OpenLakeForge does not sign the person out of GitHub; on a
  shared browser the next sign-in reuses that GitHub account (ADR 0014,
  "Logout").
- The cluster must reach `github.com` and `api.github.com` for logins and
  refreshes, an egress flow ADR 0017 does not have yet. A GitHub
  outage blocks new logins; existing sessions run to their bound.
- Air-gapped installations are not served by the default adapter; they use
  `issuer: external`.
- `olf e2e run` cannot sign in to real GitHub unattended (accounts, 2FA). The
  Dex adapter's e2e needs a stub upstream in kind, and a qualification against
  a real organisation is a separate, manual or scheduled check.

## History

New record. Collects the GitHub dependencies the repository already had
(ADR 0009 publishing, the CI workflows, GHCR) and decides GitHub as the
source of people and roles, replacing the self-hosted user directory of #331.
