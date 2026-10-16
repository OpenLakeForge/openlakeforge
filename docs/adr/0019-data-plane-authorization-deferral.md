# ADR 0019: Data-plane authorization is deferred

## Status

Decided. **Binding today:** v0.5-alpha has service-level authorization only;
nothing in it restricts which tables, rows or columns a signed-in user reads.
**Assessed, not built:** the per-user path through Trino impersonation and
Superset. The follow-up is #367 (v1.0), sequenced ahead of the governance
work (#54-#57).

Every claim below carries a mark. **V** is verified against the cited
documentation or source (read, not run). **T** is to test: derived or assumed,
and not exercised here. Pins are from `release/component-catalog.yaml`: Trino
480 (chart 1.42.2), Superset 6.1.0 (chart 0.15.5), Polaris 1.4.0.

## Context

The canonical role decides which services a person reaches (ADR 0014). It does
not decide what they read once inside. Superset connects to Trino as one
shared principal per stage (ADR 0016, `identity-callers.md`), and Trino reaches
the catalog and object storage with its own credentials (ADR 0015). The end
user's identity never leaves Superset, so every Superset user has the data
access of that one principal.

Stated plainly for operators: **in v0.5-alpha, any user who can reach Superset
is limited in data only by what Superset's Trino connection can read.** The
platform's role does not narrow it. Do not load data of differing sensitivity
into a deployment whose users should not all see it.

## Decision

1. **Defer.** v0.5-alpha ships service-level authorization only, and says so in
   the release notes (`CHANGELOG.md`) and `identity-callers.md`.
2. **Do not foreclose.** The assessment below finds no v0.5 decision that rules
   out per-user data access. Two are accepted costs (foreclosure review).
3. **Enforce at Trino.** When built, per-user policy is Trino file-based access
   control keyed by canonical role, the one point that is identical for
   Polaris and Glue (ADR 0015). Catalog and storage policy are defence in
   depth, not the first line.
4. **Superset propagates identity by impersonation**, not by one connection
   per user, as the first candidate (below). #367 confirms or replaces it.

## Trino 480: impersonation

| # | Claim | Mark | Reference |
| --- | --- | --- | --- |
| 1 | The session user is the `X-Trino-User` header if sent, otherwise the authenticated user. With authentication the authenticated identity is kept as the principal and only the user is replaced | V | `HttpRequestSessionContextFactory.buildSessionIdentity`, tag 480 |
| 2 | Two checks run on a request. `checkCanSetUser(principal, user)` always; `checkCanImpersonateUser(authenticated identity, user)` only when the authenticated user differs from the requested one | V | `HttpRequestSessionContextFactory.extractAuthorizedIdentity`, tag 480 |
| 3 | File-based `impersonation` rules take `original_user`, `original_role`, `new_user` (required, may use captured groups) and `allow`; the first match wins and no match denies | V | Trino 480 docs, file-based access control, "Impersonation rules" |
| 4 | With no `impersonation` rules but `principals` rules present, impersonation is allowed (the principal rules are assumed to govern it). With neither, it is denied | V | same page |
| 5 | `principals` rules are marked **deprecated** in 480; the docs point to user mapping plus `impersonation` rules | V | same page, "Principal rules" |
| 6 | Today's mechanism (ADR 0016: `principals` rules, a DEV credential acting as `alice` on DEV only, `cannot become user bob` otherwise) was run on 480 | V | ADR 0016 cases g1-g3 |
| 7 | Replacing the `principals` rules with `impersonation` rules gives the same stage binding: a DEV credential asking for `olf-prod-runtime` matches no rule and is denied, and a request for its own user skips the check | T | derived from claims 2-4; not run |
| 8 | Rules can match `group`; groups come from a group provider, which is `file` or `ldap`. The OAuth2 authentication page lists no option that reads groups from a token | V | Trino 480 docs, group mapping; OAuth2 authentication |
| 9 | Iceberg table data is read with Trino's own S3 key and the catalog is created without vended credentials, so Trino rules are the only per-user check on data files | V | ADR 0015, `modules/query/trino/main.tf` |

### How a verified workload credential impersonates an end user

1. Superset authenticates to Trino as `olf-<stage>-runtime` (password, ADR
   0016), the verified principal.
2. It sends the signed-in user's name as `X-Trino-User`. Trino sees an
   authenticated user that differs from the requested one (claim 2).
3. An `impersonation` rule lets exactly that principal become end users:
   `original_user: olf-<stage>-runtime`, `new_user` matching the end-user name
   pattern. Catalog, schema and table rules then evaluate the end user.

What this means for the design:

- **Trino trusts Superset's assertion.** The credential proves which service
  is calling, not which person. The guarantee is only as strong as Superset's
  own authentication and its refusal to let a client choose the name (Superset
  section, claim 3).
- **Deny the service names first.** Rules are first-match (claim 3), so a
  `deny` rule for every service principal (`olf-*-runtime`, `openmetadata`)
  must precede the allow, or a stage credential could become another stage's
  principal. T: the ordered rule set and its isolation cases.
- **End-user names must not collide with service names.** GitHub logins are
  free text (ADR 0018) and could equal a service principal. The Trino user
  needs a namespace or a pattern the service names cannot match. T.
- **Roles reach Trino only through a group provider** (claim 8). The
  `groups` claim from Dex (ADR 0018) is read by the perimeter and Superset,
  not Trino. A Trino group provider fed from the same canonical
  role mapping, or user-keyed rules generated from it, is the "fifth mapping
  keyed by canonical role" ADR 0014 reserves. T: which of the two.

## Superset 6.1.0: per-user propagation

| # | Claim | Mark | Reference |
| --- | --- | --- | --- |
| 1 | A database has an `impersonate_user` flag; the imported Trino connections set it `false` | V | `superset/models/core.py`; `lakehouse_code/dashboards/superset/*/databases/openlakeforge_trino.yaml` |
| 2 | When set, the Trino engine spec puts the effective username in the driver's `user` connect argument | V | `superset/db_engine_specs/trino.py`, `impersonate_user` |
| 3 | The effective user is the logged-in user's name from the request context, not client input; with no request context (no `g.user`) it falls back to the connection URL's user, i.e. the shared principal | V | `core.py`, `Database.get_effective_user` |
| 4 | The feature flag `IMPERSONATE_WITH_EMAIL_PREFIX` swaps the name for the part of the user's email before `@` | V | `core.py`, `_get_sqla_engine` |
| 5 | The `trino` driver sends its `user` argument as `X-Trino-User` | T | driver not read |
| 6 | The Trino engine spec sets `supports_oauth2`. With a per-database OAuth2 configuration, the user's token for that database is sent as an `Authorization: Bearer` header alongside the user name | V | `trino.py`, `impersonate_user`; `core.py`, `get_oauth2_access_token` |
| 7 | That OAuth2 token comes from a flow configured on the database (`oauth2_client_info`), not from the SSO session. Whether the SSO session token can serve is not established | T | `core.py` read; no deployment |
| 8 | Using claim 6 needs Trino to accept the token: an `OAUTH2` or `JWT` authenticator beside `PASSWORD`, with the token's user name matching a Trino rule. ADR 0016 already needs `OAUTH2` for the Trino UI | T | Trino side not built |
| 9 | Result caching: the cache key includes the user only when the database impersonates **and** `CACHE_IMPERSONATION` is on, or `CACHE_QUERY_BY_USER` is on, or the database sets `per_user_caching`. `CACHE_IMPERSONATION` defaults to `False` | V | `superset/common/query_object.py`; `superset/config.py` |

### Cost

| Area | Finding | Mark |
| --- | --- | --- |
| Correctness | Turning on `impersonate_user` without `CACHE_IMPERSONATION` serves one user's cached result to another. The flag is a prerequisite, not a tuning option | V (claim 9) |
| Cache sharing | With the user in the key, a dashboard's cache is per person. Hit rate falls by roughly the number of distinct viewers per dashboard and Redis use rises the same way. No measurement | T |
| Connection pooling | Connections are created with `NullPool` by default and the engine is built on each use, so there is no pool for impersonation to defeat. Trino's protocol is per-query HTTP | V (code); T (cost) |
| Trino per query | One extra impersonation check and per-user rule evaluation. Expected negligible beside query time; not measured | T |
| Non-request contexts | Scheduled reports, alerts and cache warm-up have no `g.user` (claim 3) and run as the shared principal, so they see all stage data. Which Superset paths hit this is not established | V (fallback); T (paths) |
| Metadata browsing | Whether SQL Lab and dataset editors list only tables the user may read, and whether metadata caches are shared across users | T |
| Username source | The name is Superset's account name. Its derivation from Dex (ADR 0018) may not match the Trino rule pattern, and `IMPERSONATE_WITH_EMAIL_PREFIX` would change it again | T |

## Foreclosure review

| v0.5 decision | Rules out per-user access? | Why |
| --- | --- | --- |
| Shared per-stage Trino principal (ADR 0016) | No (avoided) | The principal is the verified service identity that impersonation rules authorise. Cases g1-g3 show it acting as other users. Keeping it is the deferral itself |
| Password-file authentication, not JWT (ADR 0016) | No | Impersonation needs only the service credential. Superset OAuth2 passthrough (claim 8) adds an authenticator beside `PASSWORD`; it does not replace it |
| `principals` rules (ADR 0016) | No, but accepted debt | Deprecated in 480 (claim 5) and not the shape to extend. #367 replaces them with `impersonation` rules and re-runs the isolation cases (claim 7) |
| GitHub teams and Dex (ADR 0018) | No (avoided) | Roles stay canonical (ADR 0014). Cost: the `groups` claim does not reach Trino (claim 8), so a role-to-group mapping is added, and GitHub logins need a namespace |
| Keycloak replaced by Dex | No | Nothing per-user is stored in Trino, Superset or the catalog. A rename of users costs nothing before beta |
| Trino reads storage with its own key (ADR 0015) | Storage-level policy: **accepted** | Vended credentials stay off, so only Trino enforces per-user policy. Anything with the bucket credentials (Floe, dbt, Dagster) is outside it by design |
| Polaris `internal` authentication (ADR 0015) | No | Polaris sees the stage principal. Per-user identity at the catalog is optional defence in depth, not a prerequisite for Trino policy |
| Catalogs `lakehouse_<stage>` with catalog and table rules | No | The rules file already has `tables` rules; per-user rules are added, not restructured |
| OpenMetadata reads every stage catalog (ADR 0016, `read-only`) | Metadata and sample data: **accepted** | It is a service principal. What an OpenMetadata user sees is governed by #56, not by Trino policy. Not assessed here |
| Imported Superset connections set `impersonate_user: false` | No | A report-import change flips it. The cache flag must flip with it (Superset claim 9) |
| Local NetworkPolicy (ADR 0017) | No | Network-level only |

## Prerequisites for per-user data access (#367)

1. Trino authentication enforced (ADR 0016, migration step 4) before any
   impersonation: an unauthenticated Trino accepts any `X-Trino-User`.
2. `principals` rules replaced by ordered `impersonation` rules, service names
   denied first; isolation cases extended and run on 480 (Trino claim 7).
3. A stable, collision-free end-user name from Dex to Superset to Trino.
4. A role-to-Trino mapping keyed by canonical role: group provider or generated
   user rules (ADR 0014, fifth mapping), with table-level rules per role.
5. Superset: `impersonate_user` on with `CACHE_IMPERSONATION` on, a decision on
   `IMPERSONATE_WITH_EMAIL_PREFIX`, and a policy for contexts with no user.
6. A measurement of the dashboard cost on kind with several users (Superset
   cost table).
7. A decision on the catalog layer: Polaris session identity (ADR 0015) and
   Glue with Lake Formation stay separate adapters.
8. `olf e2e` cases: an `analyst` denied a table a `data-engineer` reads, through
   Superset, and a cache check that one user's result is not served to another.

## Consequences

- No code, Terraform or contract changes.
- The release notes say what the platform does not do and what to avoid
  loading; they make no claim of data-level authorization.
- The ADR 0016 `principals` choice is knowingly on a deprecated Trino feature
  until #367.

## History

New record (#179): the deferral, the Trino 480 and Superset 6.1.0 assessments,
the foreclosure review and the prerequisites.
