# ADR 0016: Trino workload authentication — none; Polaris decides

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ (#983) · AWS ✘: claim-based rules for Glue until #681 · Azure: one shared coordinator, keyless |

## Context

Trino once authenticated workloads because one coordinator held every stage's
Polaris credential and a static S3 key, so it had to tell its callers apart
itself: a password per stage principal, `principals` rules binding it to one
stage user, and an HTTPS listener, because Trino authenticates only a request
it considers secure (#932). ADR 0019 D8 and D9 removed both causes on the
local root: one coordinator per stage (#976) makes the stage boundary a
network boundary, and the keyless catalogs (#982) leave Trino no credential
with a grant and no storage key, so Polaris decides every table access from
the token each caller sends.

## Decision

**D1. No workload authentication.** The local root's coordinators serve plain
HTTP on 8080 only. Every caller sends its own Polaris token as the `token`
extra credential (dbt and Gold maintenance, `olf infra query` and `olf infra
e2e run`, `olf query` and the platform API, Superset per user, the
OpenMetadata crawl). The user Trino records is a claim: it labels the query
and never changes what Polaris allows (#964). Enforced by:
`infra/terraform/modules/query/trino`.

**D2. No access rules on a keyless catalog.** A Glue catalog is not keyless,
so AWS's shared coordinator keeps the claim-based `catalog_access_rules`
until AWS moves to Polaris (#681); nothing else renders them. Enforced by:
`catalog_access_rules` in `infra/terraform/modules/query/trino/main.tf`.

**D3. The Web UI signs in as a fixed user** (`web-ui.authentication.type=fixed`)
behind the perimeter, which keeps its `data-engineer` floor. The UI shows
every query; what a query reads is still Polaris's decision. Enforced by:
`infra/terraform/modules/query/trino/ui.tf`.

**D4. Stage isolation is the Polaris grant of the presented token plus the
NetworkPolicy of ADR 0017.** A stage's coordinator is reachable from its own
namespace, Traefik, OpenMetadata and the platform API only. Enforced by:
`release/network-flows.yaml`.

## Consequences

- The user in Trino's query log is a claim: access is decided by the token
  alone, and Polaris logs the principal `github`, not the person (ADR 0019
  D7).
- Anyone who passes the perimeter's Web UI floor sees every query in the UI.
  The UI shows SQL and timings, not rows; reading data still needs a token
  Polaris accepts.
- AWS and Azure keep one shared coordinator until their cloud milestone moves
  them; AWS keeps claim-based rules for Glue until then (`docs/technical-debt.md`).

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Password-file authentication over an HTTPS-only listener, `principals` rules and a credential Job per stage (#26, #471) | Built, then removed (#983): its causes, a shared coordinator holding every stage's credential, are gone (ADR 0019 D8, D9) |
| `PASSWORD` on the plain HTTP listener with `http-server.authentication.allow-insecure-over-http=true` (#932) | Trino skips its authenticators on a request it does not consider secure: a request with no credential ran as the `X-Trino-User` it named, and a password was refused |
| `JWT` beside `PASSWORD` for people's Dex tokens, and per-person catalogs with their own rules (#674) | Removed with D1: Polaris decides from the token, so Trino needs no rule |
| Web UI sign-in through Dex (OAuth2) | Removed with D1; the perimeter guards the route (D3) |

## History

- 2026-10-23 (#26, #471): New record. Password-file
  authentication over an HTTPS-only listener, with `principals` rules per
  stage principal; the Web UI signed in through Dex on HTTPS, or as a fixed
  user in plain-HTTP mode.
- 2026-11-07 (#960): Scheduled for retirement: ADR 0019 D3 makes
  Polaris the authority for workloads as well as people.
- 2026-11-09 (#983): Rewritten in place: D1 to D4 replace the password
  mechanism; Trino authentication, its rules, the HTTPS listener and the Web
  UI's OAuth2 sign-in are removed from the local root.
- 2026-11-22 (#1384): Rewritten in the standard form; no decision changed.
