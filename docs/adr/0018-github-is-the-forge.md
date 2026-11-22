# ADR 0018: GitHub is the forge — source, CI/CD, packaging and people

| | |
| --- | --- |
| Status | Binding |
| Built | Source, CI, release, GHCR ✔ · people and roles from GitHub teams through Dex: local ✔ (#384) · client CD ✘ (`olf init` scaffolds no workflow) |

## Context

The audience is a small data team without a platform engineer. Such a team
already keeps its repository, its CI runners, its container images and the
list of who works there in one place: its forge. Every system OpenLakeForge
adds beside that one is a second place to onboard and offboard people, to
rotate a credential, and to look when something breaks. A user directory of
OpenLakeForge's own (#331) duplicated the membership the team already keeps
in GitHub and made offboarding two actions, one of them easy to forget.

## Decision

**D1. GitHub is the one forge OpenLakeForge targets.** A client project is a
GitHub repository holding `openlakeforge.yaml` and `lakehouse_code/` (ADR
0009, project root), and OpenLakeForge reuses what that repository already
has rather than standing up a parallel system. Other forges are out of scope;
see D7.

**D2. This repository's CI runs on GitHub Actions.**
`.github/workflows/checks.yml` runs on every pull request and `merge_group`;
`main` sits behind the `main_protection` ruleset and a merge queue. Enforced
by: `checks.yml` and the ruleset.

**D3. This repository releases through GitHub.** PyPI through GitHub OIDC
Trusted Publishing, and GitHub Releases with checksums, SBOMs and provenance
(ADR 0009). Enforced by: `.github/workflows/release.yml`.

**D4. Project images go to GHCR for this repository.** `local-full-e2e.yml`
pushes the nightly `project-code-nightly` image there, and
`docs/guides/promote-dev-to-prod.md` documents GHCR (or Docker Hub) for
promotable revisions. A client's registry stays an environment setting.

**D5. A client project's CD is a GitHub Actions workflow.** It runs `olf
infra project image`, `olf infra project build` and `olf infra project
deploy`, the steps `.github/actions/build-revision` already chains, on the CD
boundary of ADR 0002. Not built: `olf init` scaffolds no workflow yet.

**D6. People and roles come from GitHub organisation membership and teams,
and GitHub is where they are administered.** Dex brokers a GitHub login into
the `identity.oidc` contract of ADR 0014, and each canonical role maps to one
or more teams. Inviting, removing and changing someone's team is done in
GitHub (web, `gh`, or the client's own GitHub Terraform); OpenLakeForge has
no user-administration command. Dex is used rather than GitHub OAuth in each
service because GitHub's user login is OAuth 2.0 without OpenID Connect: it
issues no ID token and no groups claim (GitHub's OIDC tokens exist only for
Actions workloads). Wiring Superset, OpenMetadata, the Trino UI and the
perimeter to GitHub's API directly would put a provider-specific adapter in
every consumer; Dex's GitHub connector instead issues an OIDC token whose
`groups` claim lists the user's teams as `<org>:<team-slug>`, so the contract
and the role mapping stay unchanged. Dex keeps no user directory: only
refresh tokens and signing keys, in its own database on the platform
PostgreSQL. Enforced by: `infra/terraform/modules/identity/dex`.

**D7. Another forge is an issuer, not a new identity system.**
`spec.identity.issuer: external` accepts any OIDC issuer, and Dex has
connectors for GitLab, Google and Microsoft, so supporting another forge's
people is a connector.

## Consequences

- Inviting, offboarding and changing a role need no OpenLakeForge command and
  no apply: they are GitHub team changes, under the client's existing GitHub
  audit log and 2FA policy. The first administrator is whoever is in the team
  mapped to `platform-admin`.
- Every platform user needs a GitHub account in the mapped organisation. On a
  paid GitHub plan each member is a seat, which matters for dashboard viewers;
  a separate free organisation used only for platform access is an option.
- Signing out of OpenLakeForge does not sign the person out of GitHub; on a
  shared browser the next sign-in reuses that GitHub account (ADR 0014,
  "Logout").
- The cluster must reach `github.com` and `api.github.com` for logins and
  refreshes (flow F28 in `release/network-flows.yaml`). A GitHub outage blocks
  new logins; existing sessions run to their bound.
- Air-gapped installations are not served by the default adapter; they use
  `issuer: external`.
- `olf infra e2e run` cannot sign in to real GitHub unattended (accounts,
  2FA), so the Dex adapter's e2e uses a stub upstream in kind, and a
  qualification against a real organisation is a separate, manual or
  scheduled check.

## Boundaries

| Not decided here | Where |
| --- | --- |
| How canonical roles map to teams, revocation latency, break-glass | ADR 0014 and `docs/architecture/identity/identity-sessions-and-recovery.md` |
| Workload credentials (Trino, Polaris, object storage) | ADR 0015, ADR 0016, `identity-callers.md`; GitHub is never a workload credential inside the cluster |
| GitHub Actions OIDC as the credential CI uses to reach the platform | Not decided; CI keeps Kubernetes RBAC through kubeconfig (`identity-callers.md`, class 2) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| A user directory of OpenLakeForge's own (Keycloak, email invitations, SMTP, `olf users`; #331) | Duplicates the membership the team keeps in GitHub; offboarding becomes two actions |

## History

- 2026-10-16 (#383): New record. Collected the GitHub
  dependencies the repository already had (D1 to D4) and decided D5 to D7,
  replacing the self-hosted user directory of #331; `olf users`, the
  `shared.identity.admin` contract, the Keycloak `olf-users` client and
  `spec.identity.bootstrap_admins` removed.
- 2026-11-22 (#1384): Rewritten in the standard form; the Status now records
  D6 as built (Dex adapter, #384); no decision changed.
