# Superset and OpenMetadata: role permissions and session alignment

The specification #25 implements. It fixes, per canonical role, what each
service must allow and deny, the native objects that implement
`release/identity-roles.yaml`, how roles reach each service, and how each
service meets the session policy in
[`identity-sessions-and-recovery.md`](identity-sessions-and-recovery.md).
Nothing here is built. No cluster was available, so every runtime claim is
marked.

| Mark | Meaning |
| --- | --- |
| **V** | Read in the source or docs of the pinned version (source named) |
| **T** | To verify on the running stack; the test plan names the check |
| **D** | Design decision proposed here; maintainer to confirm |

Pinned versions (`release/component-catalog.yaml`): Superset image 6.1.0 and
chart 0.15.5; OpenMetadata chart and server 1.13.6. Read: Superset 6.1.0
`superset/security/manager.py`, `config.py`, `dashboards/filters.py`,
`requirements/base.txt` (Flask-AppBuilder 5.0.2, Flask 2.3.3); Flask-AppBuilder
5.0.2 `security/{manager,views,api}.py`; Flask 2.3.3 `sessions.py`; chart
`superset-helm-chart-0.15.5`; OpenMetadata `1.13.6-release` `conf/openmetadata.yaml`,
`security/{JwtFilter,AuthenticationCodeFlowHandler,DefaultAuthorizer}`,
`policyevaluator/SubjectContext`, `util/UserUtil`, `jdbi3/TeamRepository`,
`OpenMetadataApplication`, `json/data/{role,policy}/*`; chart
`openmetadata-1.13.6` `values.yaml`, `templates/secrets.yaml`.

## Findings that change the plan

| # | Finding | Source | Consequence |
| --- | --- | --- | --- |
| 1 | Superset has no read-only dashboard role. `Gamma` can create and edit its own charts and dashboards | **V** `manager.py` `_is_gamma_pvm` | `viewer` is a custom role: Superset's own `Public` permission set under another name |
| 2 | `superset init` rewrites `Admin`, `Alpha`, `Gamma` and `sql_lab` permissions from code. Data-access grants (`schema_access`) added by hand to `Gamma` are dropped | **V** `set_role`, `_is_user_defined_permission` | Data scope for `analyst` and `viewer` lives in a separate custom role |
| 3 | `Alpha` has no SQL Lab. SQL Lab permissions belong to the `sql_lab` role only | **V** `_is_alpha_pvm` excludes `_is_sql_lab_only` | The yaml maps `data-engineer` to `Alpha`, so data engineers have no SQL Lab. Open question 1 |
| 4 | Flask-AppBuilder's built-in `keycloak` provider reads the `groups` claim by name | **V** `get_oauth_user_info` | Unusable: it is the Keycloak-specific claim the contract forbids. A custom user-info method reads `role_claim` |
| 5 | The login page waits for a click: FAB's `AuthOAuthView.login` lists providers, and Superset registers its own `SupersetAuthView` at `/login/` unless a subclass sets `register_superset_auth_view = False` | **V** `AuthOAuthView`, `views/auth.py`, `register_views` | Silent renewal needs a small auth-view subclass registered through `authoauthview` |
| 6 | OpenMetadata has no roles named `read` or `write`; `Admin` is a user flag. `DataConsumer`, which every user inherits, is not read-only | **V** `UserUtil`, `TeamRepository.initOrganization`, seed policies | `read` and `write` are defined below; the default baseline must be tightened |
| 7 | OpenMetadata's provider role sync reads a claim literally named `roles` whose values are OpenMetadata role names. It grants `Admin` but never removes it, and an empty claim changes nothing | **V** `JwtFilter`, `UserUtil.reSyncUserRolesFromToken` | Cannot consume the canonical claim, and cannot demote. The fix to remove roles is on `main` after 1.13.6 |
| 8 | With `custom-oidc`, `POST /api/v1/users/login` answers 403 | **V** `registerAuthenticator`, `NoopAuthenticator` | `admin@open-metadata.org` / `admin` stops working by construction; every `olf` and bootstrap path that uses it must move to a bot token |
| 9 | OpenMetadata's HTTP session lifetime has a floor of 1 h and is an idle timeout | **V** `OpenMetadataApplication.registerAuthServlets` | The 15 min non-sliding session cannot be set. Section "Sessions" shows why the session is not what carries authority |

## Superset

### Native roles in 6.1.0

| Native role | What it is | Source |
| --- | --- | --- |
| `Admin` | Every permission except per-object data grants. Role name is `AUTH_ROLE_ADMIN` | **V** `_is_admin_pvm`, `is_admin` |
| `Alpha` | Everything not admin-only and not SQL Lab, including `all_datasource_access` and `all_database_access`. Writes datasets, imports and exports, alerts and reports, CSV upload | **V** `_is_alpha_pvm`, `ALPHA_ONLY_*` |
| `Gamma` | `Alpha` minus alpha-only items. Writes charts and dashboards, reads datasets, sees only data granted to it | **V** `_is_gamma_pvm`, `GAMMA_READ_ONLY_MODEL_VIEWS` |
| `sql_lab` | SQL Lab only (`SQLLAB_ONLY_PERMISSIONS`) | **V** |
| `olf_viewer` (custom) | `PUBLIC_ROLE_PERMISSIONS`: read dashboards and charts, dashboard filter state, chart data. Excludes SQL Lab, saved queries, security, import, annotation layers, CSS templates, alerts | **V** `PUBLIC_ROLE_PERMISSIONS`, `PUBLIC_EXCLUDED_VIEW_MENUS` |
| `olf_data_access` (custom) | Only `schema_access` grants for the product Gold schemas. Held by `analyst` and `viewer` | **D** |
| `olf_no_access` (custom) | No permissions. The registration role for users with no recognised role | **D** |

`Admin` and `Alpha` see all data. `Gamma` and `olf_viewer` see a dashboard only
if it is published and the user can access its datasets, or the user owns it
(`DashboardAccessFilter`, **V**). The imported dashboards are `published: true`
(`lakehouse_code/dashboards/superset/*/dashboards`).

### Creating `olf_viewer` and the data scope (D)

| Piece | How |
| --- | --- |
| Custom security manager | `CUSTOM_SECURITY_MANAGER` subclass of `SupersetSecurityManager`. `sync_role_definitions` is overridden: call the parent, then `set_role("olf_viewer", self._is_public_pvm, pvms)` and create `olf_no_access` empty. `superset init` runs in the chart init Job on every install and upgrade (**V** chart `init.initscript`; that `init` calls `sync_role_definitions` is **T**), so the roles are declarative and re-created after drift. `_is_public_pvm` is private API: a unit test must assert the resulting permission list, so a Superset bump that changes it fails loudly |
| Data scope | `olf_data_access` holds `schema_access` on `[<database>].[<schema>]` (**V** `get_schema_perm` format when no catalog is set). `olf report import` already holds an Admin client; after importing it ensures each imported dataset's schema is in the role through the FAB role-permission REST API (`FAB_ADD_SECURITY_API` is on by default, **V** `config.py`; the exact endpoint is **T**). The schema list comes from the imported dataset files, not from a list in code (AGENTS rule 5) |
| Why not `DASHBOARD_RBAC` | It would need a per-dashboard role assignment at import; schema scope is one grant per schema. Revisit if per-dashboard audiences are wanted |
| `Public` role | Not used. `Public` is also the anonymous role; a separate name keeps unauthenticated access impossible by construction |

### Allowed and denied actions

Columns are the canonical role and the native roles its users hold (**V** from
`release/identity-roles.yaml` plus the table above). Cells are the target; the
basis is the source rule named on the right.

| Action | platform-admin (`Admin`) | data-engineer (`Alpha`) | analyst (`Gamma` + `olf_data_access`) | viewer (`olf_viewer` + `olf_data_access`) | Basis |
| --- | --- | --- | --- | --- | --- |
| View published dashboards | Allow, all | Allow, all | Allow, granted schemas | Allow, granted schemas | `DashboardAccessFilter`, `can_access_all_datasources` |
| Create and edit own charts and dashboards | Allow | Allow | Allow, on granted datasets | **Deny** | `can_write Chart/Dashboard` in Admin, Alpha, Gamma; not in `PUBLIC_ROLE_PERMISSIONS` |
| Create and edit datasets | Allow | Allow | **Deny** | **Deny** | `Dataset` in `GAMMA_READ_ONLY_MODEL_VIEWS` |
| SQL Lab | Allow | **Deny** | **Deny** | **Deny** | `SQLLAB_ONLY_PERMISSIONS` only in Admin and `sql_lab`; `PUBLIC_EXCLUDED_VIEW_MENUS` |
| Add, edit, delete database connections | Allow | **Deny** | **Deny** | **Deny** | `Database` in `READ_ONLY_MODEL_VIEWS`: non-read permissions admin-only |
| Import and export dashboards, alerts and reports, CSV upload | Allow | Allow | **Deny** | **Deny** | `ALPHA_ONLY_VIEW_MENUS`, `ALPHA_ONLY_PMVS` (upload also needs the database's `allow_file_upload`) |
| Row-level security rules | Allow | **Deny** | **Deny** | **Deny** | `ADMIN_ONLY_VIEW_MENUS` |
| User and role administration, access requests, action log | Allow | **Deny** | **Deny** | **Deny** | `ADMIN_ONLY_VIEW_MENUS`, `ADMIN_ONLY_PERMISSIONS` |

Data scope is not enforced below Superset: every Superset query reaches Trino
as the stage runtime principal ([`identity-callers.md`](identity-callers.md),
known limitations). `schema_access` limits what the Superset UI offers, not
what a connection can read.

### Claim to role (D, mechanism **V** from FAB 5.0.2)

| Setting | Value |
| --- | --- |
| `AUTH_TYPE` | `AUTH_OAUTH`, one provider named `olf`, Authlib `server_metadata_url` from `issuer_url`. `authlib` is added to `images/superset/Dockerfile`; it is not in Superset's `requirements/base.txt` (**V**) |
| Client secret | `secretKeyRef` env from `identity.oidc.clients.superset.secret_ref`; the `configOverrides` text reads `os.environ`, nothing inline |
| User info | Override `oauth_user_info`: read the claim named by `role_claim` from the ID token, string or list, and return it as `role_keys`. No claim name appears in Superset code except through the contract. The username is namespaced (`oidc-<preferred_username>`): `auth_user_oauth` finds a user by username and, with sync on, rewrites its roles (**V**), so an issuer user named `admin` must not resolve to the local `admin` account |
| `AUTH_ROLES_MAPPING` | Expanded from the contract: every claim value of `role_mapping[r]` maps to the grant of `r`: `platform-admin` to `["Admin"]`, `data-engineer` to `["Alpha"]`, `analyst` to `["Gamma", "olf_data_access"]`, `viewer` to `["olf_viewer", "olf_data_access"]`. Several roles give the union (**V** `get_roles_from_keys`), consistent with monotonic grants |
| `AUTH_ROLES_SYNC_AT_LOGIN` | `True`: a returning user's roles are replaced at every login, so a removed role is removed (**V** `auth_user_oauth`) |
| `AUTH_USER_REGISTRATION` | `True`; `AUTH_USER_REGISTRATION_ROLE = "olf_no_access"`. FAB adds this role on top of mapped ones for every user (**V** `_oauth_calculate_user_roles`), so it must carry nothing |
| No recognised role | The perimeter answers first with the role-restricted 403 (ADR 0014). Natively: override `auth_user_oauth` to return no user when the mapped set is empty, and override the auth view's failure path to render a 403 page ("no OpenLakeForge role assigned"). It must not redirect to `/login/`, which would loop through the silent renewal below (**V** `oauth_authorized` redirects on failure) |

### Local administrator and automation (class 2)

| Item | Today | Target |
| --- | --- | --- |
| `admin` / `admin` login form | Chart `init.createAdmin` with a literal password | Under `AUTH_OAUTH` the browser path has no password handler: `SupersetAuthView` serves GET only and `AuthOAuthView` has no password route (**V**); that the page shows no form is **T**. Keep a database user only for automation |
| REST login used by `olf report import`, e2e, and the OpenMetadata Superset connection | `provider: "db"` on `/api/v1/security/login` | **V** `api.py` selects the `db` provider without checking `AUTH_TYPE`, so it keeps working. **T** on the running stack |
| Identity | `admin` | A dedicated `olf-automation` Admin user for import and e2e; a separate Alpha user for the OpenMetadata connection (read-only endpoints; **T** which role suffices). Passwords from a Secret created by the bootstrap Job (#181), referenced by `secretKeyRef`. The chart's `create-admin ... \|\| true` does not change an existing password, so the init script must use `fab reset-password` or a new user (**T**) |
| Collision | n/a | Role sync never touches database users (**V** it runs only in `auth_user_oauth`), and the namespaced OIDC username keeps the two apart |

### Sessions (D, mechanism **V**, behaviour **T**)

The cookie is a signed Flask session. Flask 2.3.3 rejects any session cookie
older than `PERMANENT_SESSION_LIFETIME`, permanent or not, and re-signs the
cookie whenever the session is modified or, for permanent sessions, on every
request when `SESSION_REFRESH_EACH_REQUEST` is true (**V** `open_session`,
`should_set_cookie`). Superset does not set either option, so the default
lifetime is 31 days.

| Setting | Value | Why |
| --- | --- | --- |
| `PERMANENT_SESSION_LIFETIME` | 15 min | Upper bound on cookie age |
| `SESSION_REFRESH_EACH_REQUEST` | `False` | Stops the every-request refresh |
| Absolute-age check | At login store `olf_login_at` in the session; a `before_request` hook logs the user out when older than 15 min | Re-signing on any session write (CSRF token, flash message) would otherwise slide the bound. The stored value is inside the signed cookie, so replaying an old cookie fails the check. This is what makes the bound non-sliding |
| Auto-redirect | In the custom security manager: `register_superset_auth_view = False` and `authoauthview` set to a subclass whose `login()` with no provider redirects to `/login/olf` | Expiry sends the user through the issuer, which answers silently while the SSO session lives, and `AUTH_ROLES_SYNC_AT_LOGIN` re-reads the claim. Without it the user clicks a provider button |
| Logout | `/logout/` is in the platform logout chain (#176) | Policy: platform-wide |

Bounds for Superset: role **change** (demotion) takes effect within 15 min
(forced re-login). **Disable** and role **removal**: the perimeter refuses within
5 min, before the native session matters. **T**: that continuous activity does
not extend the session, and that expiry and re-login are invisible to the user.

## OpenMetadata

### Native roles and policies in 1.13.6

| Canonical grant | Native implementation | Source |
| --- | --- | --- |
| `Admin` | The user flag `isAdmin`. Not a role row. `authorizeAdmin` checks `subject.isAdmin()`; authorization reads the database user (`roles`, `teams`, `isAdmin`), never the token | **V** `DefaultAuthorizer`, `SubjectContext` |
| `write` | Built-in role `DataSteward`, policy `DataStewardPolicy`: `ViewAll`, `EditDescription`, `EditDisplayName`, `EditLineage`, `EditOwners`, `EditTags`, `EditTier`, `EditGlossaryTerms`, `EditCertification`, `Trigger`. No `Create`, no `Delete` | **V** seed `DataSteward.json`, `DataStewardPolicy.json` |
| `read` | **No built-in role.** `DataConsumer` is not read-only: it adds `EditDescription`, `EditTags`, `EditGlossaryTerms`, `EditTier`, `EditCertification` to `ViewAll`. Read-only is the Organization baseline with the two changes below, and no user-level role | **V** `DataConsumerPolicy.json`, `OrganizationPolicy.json` |

Baseline: every user belongs to team `Organization`, created with policy
`OrganizationPolicy` and **default role `DataConsumer`**, so every user
inherits `DataConsumer` (**V** `TeamRepository.initOrganization`). The policy
also lets anyone set the owner of an unowned entity (`NoOwner-Rule`), and an
owner holds every operation on the entity (`Owner-Rule`). Left alone, an
`analyst` or `viewer` can edit descriptions, tags and tiers on all assets and
take ownership of any unowned one, which is not `read`. The bootstrap Job must
therefore (**D**; that the system team and policy accept the patch is **T**):

1. set `Organization` default roles to empty;
2. remove `OrganizationPolicy-NoOwner-Rule`.

With those, `read` is exactly `OrganizationPolicy-ViewAll-Rule`.

### Allowed and denied actions

| Action | platform-admin (`isAdmin`) | data-engineer (`DataSteward`) | analyst (baseline) | viewer (baseline) | Basis |
| --- | --- | --- | --- | --- | --- |
| Discovery: search, browse, view assets and lineage | Allow | Allow | Allow | Allow | `ViewAll` in `OrganizationPolicy` |
| Edit description, tags, glossary terms, tier, certification | Allow | Allow | **Deny** | **Deny** | `DataStewardPolicy-EditRule`; denied only after baseline change 1 |
| Edit display name, lineage; run ingestion pipelines (`Trigger`) | Allow | Allow | **Deny** | **Deny** | Same rule and `TriggerRule` |
| Change owners | Allow | Allow | **Deny** | **Deny** | `EditOwners` in `DataStewardPolicy`; denied for others only after baseline change 2 |
| Create or delete entities (services, domains, glossaries, tables) | Allow | **Deny** | **Deny** | **Deny** | No `Create` or `Delete` outside admin and bot policies. Open question 3 |
| Roles, policies, teams, users, bots, settings | Allow | **Deny** | **Deny** | **Deny** | No role grants `Create` on these; `authorizeAdmin` endpoints need the flag |

Known path outside the roles: `Owner-Rule` gives an entity's owner `All` on it
whatever the owner's role. A data engineer demoted to `analyst` who still owns
assets keeps full control of them until ownership is reassigned. Open
question 4.

### Claim to role: the native mechanism cannot be used (D)

| Mechanism in 1.13.6 | Why it fails |
| --- | --- |
| `useRolesFromProvider` | Reads the `roles` claim only; values must be OpenMetadata role names, and `Admin` for the flag. Canonical values (`data-engineer`) are not those names, so an issuer-side mapper would translate them: issuer-specific configuration. `reSyncUserRolesFromToken` never clears `isAdmin` and ignores an empty claim (**V**), so an admin-to-lower-role demotion and a stale role both survive. It runs only when the UI calls `/users/loggedInUser`, no bound (**V** `UserResource`, **T** how often) |
| Team claim mapping | Adds team membership, never removes it (**V** `assignTeamsFromClaim`), and `conf/openmetadata.yaml` exposes no setting for the claim name |
| `defaultOAuthRole` | Applies once, to a new user (**V**); the chart does not expose it |

**Proposed: OpenMetadata authenticates through the issuer and `olf` assigns
roles.** The browser flow creates the user at first login
(`enableSelfSignup: true` is required, **V** `getOrCreateOidcUser`) with the
baseline only; `useRolesFromProvider` stays `false`. `olf` owns the grant:

| Step | Detail |
| --- | --- |
| Command | `olf openmetadata sync-roles [--user]`: for each issuer user, set `isAdmin` for `platform-admin`, the `DataSteward` role for `data-engineer`, and neither for `analyst` and `viewer`; the canonical role is read the way `olf users list` reads it |
| Called by | `olf users grant`, `revoke`, `disable` and `enable` after the issuer change, creating the OpenMetadata user first if it has never logged in (login finds an existing user by name, **V**); `olf deploy`; `olf e2e run` |
| Identity | The class-2 OpenMetadata automation bot below, over the RBAC-authorized port-forward |
| Gap | A role changed in the issuer console, not through `olf users`, reaches OpenMetadata at the next `sync-roles`. Documented exception, tested (test OM-7). Whether to add a scheduled run is open question 2 |

### Local administrator and automation (class 2)

| Item | Target |
| --- | --- |
| `admin@open-metadata.org` / `admin` | Unusable under `custom-oidc` (**V**, finding 8). The `admin` principal stays in the database with no credential; set `initialAdmins` to a name the issuer cannot produce, because the principal is derived from claims and `initialAdmins` users are re-flagged admin on every start (**V** `UserUtil.createOrUpdateUser`; the exact claim derivation is **T**) |
| Bot tokens | `publicKeys` keeps OpenMetadata's own JWKS URL beside the issuer's (**V** `MultiUrlJwkProvider`), so bot JWTs, which are signed by OpenMetadata, keep validating. `ingestion-bot` should keep working for Floe and Dagster (**T**, acceptance item of #25) |
| `olf` and the bootstrap Job | Replace email and password login with a dedicated admin automation bot (`olf-automation`) whose JWT is stored in a Secret created in cluster if missing (#181). `IngestionBotPolicy` denies `EditDisplayName`, so `ingestion-bot` is not a drop-in (**V**). The bot exists only after OpenMetadata starts, and creating it needs an admin token, which basic login no longer provides. **First-boot sequence is the main implementation risk (T).** Candidates: (a) install with `provider: basic`, let the Job create the bot and Secret, then upgrade the release to `custom-oidc`; (b) read the seeded bot's token from the database and decrypt it with the `fernetkey` |
| OpenMetadata to Superset | The service connection stores a Superset username and password; it moves to the Superset ingestion user above |

### Sessions (V from source, T on the stack)

OpenMetadata authenticates API calls with the **issuer's ID token** as bearer
(`JwtFilter` verifies it against the issuer's JWKS and rejects an expired one).
The server-side HTTP session holds only the refresh credentials; every
`/auth/refresh` and every login that finds credentials calls the issuer's
refresh-token grant (**V** `getUserCredentialsFromSession`, `renewOidcCredentials`).

| Setting | Value | Why |
| --- | --- | --- |
| `oidcConfiguration.sessionExpiry` | Leave default. Values under 3600 are ignored and the 7-day default applies (**V**). It is an idle timeout (**V** `setMaxInactiveInterval`) | 15 min is not reachable here, and the session holds no authority of its own |
| `oidcConfiguration.tokenValidity` | `"900"` | Only used when a refresh response has no `id_token`, when OpenMetadata mints a token itself (**V**). Default is 3600 s, which would exceed 15 min |
| `oidcConfiguration.maxAge` | `""` | Default `"0"` is sent as `max_age=0` (**V** `buildLoginParams`), which asks the issuer to re-authenticate every time (OIDC Core 3.1.2.1), defeating silent renewal |
| `oidcConfiguration.promptType` | `""` | Default `consent`; **T** Keycloak ignores it for a client without consent |
| `authentication.enableAutoRedirect` | `true` (env `AUTHENTICATION_ENABLE_AUTO_REDIRECT`, via `extraEnvs`) | No provider button |
| Client secret | `oidcConfiguration.clientSecret.secretRef/secretKey` from the contract | **V** chart renders `OIDC_CLIENT_SECRET` from a Secret |

Bounds for OpenMetadata: **disable** or session end: the next refresh at the
issuer is refused, and the ID token (5 min) and perimeter (5 min) expire first,
so at most 5 min. **T**: that Keycloak refuses the refresh for a disabled user
and that its refresh response includes `id_token`. Role **change**: bounded by
`sync-roles`, not by the session (above); a change made through `olf users` is
immediate. This is the documented exception to the 15 min native bound, and the
reason is that authorization lives in the OpenMetadata database, not in the
token.

## Test plan

Added to `olf e2e run --env local`, reusing the throwaway per-role users and
sign-in of `e2e/_identity.py` and `e2e/_perimeter.py`. Each case signs in as the
role, acts through the service's own API with that session, and asserts the
status (2xx or 403).

| ID | Actor | Action | Expect |
| --- | --- | --- | --- |
| SS-1 | viewer | List and render a published dashboard; chart data call | Allow |
| SS-2 | viewer | Create a chart; open SQL Lab; add a database; list users | Deny each |
| SS-3 | analyst | Create a chart on a granted dataset; create a dataset; open SQL Lab | Allow, deny, deny |
| SS-4 | data-engineer | Create a dataset; import a dashboard; open SQL Lab; add a database | Allow, allow, deny, deny |
| SS-5 | platform-admin | Open SQL Lab; add a database; list users | Allow each |
| SS-6 | Each role | Read the role set Superset stored for the user | Equals the table row (catches a mapped-but-wrong role) |
| SS-7 | platform-admin demoted to viewer, session open | Admin-only call after the session age passes 15 min; and with continuous activity throughout | Deny; the activity must not extend it |
| SS-8 | data-engineer role removed | Request through the perimeter; then the stale native session | Perimeter 403 within 5 min; native cookie refused by 15 min |
| SS-9 | User with no role, perimeter bypassed by port-forward | Complete the Superset login | Native 403 page, no redirect loop, no `olf_no_access` session |
| SS-10 | Two users on one browser, logout between | Second user reads first user's data | Deny (no surviving session) |
| SS-11 | Automation | `olf report import` and the OpenMetadata Superset ingestion run with SSO on | Succeed unattended |
| OM-1 | viewer, analyst | Search and read an asset | Allow |
| OM-2 | viewer, analyst | Edit description, tags, tier; set owner on an unowned asset | Deny each (proves baseline change 1 and 2) |
| OM-3 | data-engineer | Edit description, tags, owner, lineage; create a service; create a role | Allow, allow, allow, allow, deny, deny |
| OM-4 | platform-admin | Create a role; read a setting | Allow |
| OM-5 | platform-admin demoted to analyst through `olf users` | Admin-only call, immediately and after 15 min | Deny both |
| OM-6 | data-engineer demoted to viewer through `olf users` | Edit a description | Deny, with no new login |
| OM-7 | Role changed in the issuer console only | Read the user's OpenMetadata roles before and after `olf openmetadata sync-roles` | Stale before, correct after; exposure recorded |
| OM-8 | User disabled | Any call after the ID token expires | Refused within 5 min; refresh logged as refused |
| OM-9 | Anyone | `POST /api/v1/users/login` with `admin@open-metadata.org` / `admin` | 403 |
| OM-10 | Automation | `olf openmetadata deploy-metadata`, bootstrap Job, ingestion-bot run, Dagster and Floe lineage with SSO on | Succeed unattended |
| OM-11 | Issuer user whose principal is `admin` | Sign in | Not an admin |

## Open questions

1. **SQL Lab for `data-engineer`.** The yaml grants `Alpha`, which lacks SQL
   Lab (finding 3). Add `sql_lab` to the grant (a matrix change in
   `release/identity-roles.yaml` and ADR 0014), or accept no SQL Lab?
2. **OpenMetadata role assignment.** Accept `olf`-driven `sync-roles` with
   the console gap as the documented exception, or add a scheduled in-cluster
   run (a new always-on or CronJob component), or defer OpenMetadata role
   differentiation until a release that both removes roles and reads a
   configurable claim?
3. **`write` cannot create or delete entities.** `DataSteward` edits metadata
   only. Is that the intent of "write", or does `data-engineer` also create
   domains, glossaries and services (a custom role)?
4. **Ownership outlives a role.** Keep `Owner-Rule`, and reassign owned assets
   on demotion and offboarding (#177), or remove it?
5. **Patching system objects.** Is the bootstrap Job patching `Organization`
   and `OrganizationPolicy` acceptable, given an OpenMetadata upgrade may
   re-seed them?
6. **First-boot automation credential for OpenMetadata**: option (a) or (b).

## Not claimed

No behaviour in this document was run. Marked **V** items were read in the
named source at the pinned version, not executed; the OpenMetadata role-sync
decision record on `main` (2026-10-13) was read for context only, because it
postdates 1.13.6. The Keycloak side (refusing a refresh for a disabled user,
returning `id_token` on refresh, emitting the role claim in the ID token the
way each client reads it) is unverified, as it is in the session policy.
