# ADR 0015: Polaris identity-provider evaluation

| | |
| --- | --- |
| Status | Superseded by [ADR 0019](0019-data-plane-authorization-deferral.md) (2026-11-23) |
| Built | — |

This record evaluated whether Polaris should authenticate its callers with the
platform's issuer (#26). What still binds is now in ADR 0019: workloads
authenticate to Polaris with Polaris's own client credentials, never through
the issuer, and Polaris runs `internal`, or `mixed` for people's Dex tokens
(D13, D7); `external` authentication and moving workloads to issuer clients
are rejected alternatives there. Its findings on where table policy is
enforced, Polaris RBAC against Glue IAM, are ADR 0019's Context and D14. The
number is kept so that links and `git log` still resolve.

## History

- 2026-10-23 (#26): New record: Polaris external identity-provider
  evaluation, deferred for every caller, with the catalog-side findings for
  #179.
- 2026-10-31 (#674): The deferral ended for people's tokens: Polaris runs
  `mixed` with `issuer: github`.
- 2026-11-23: Superseded by ADR 0019, which absorbs its binding rule as D13.
