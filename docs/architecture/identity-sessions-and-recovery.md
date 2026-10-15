# Identity session policy and administrator recovery

The session policy #176 and #25 implement (#177), and the design of the
cluster-administrator recovery that works when Keycloak is down. Numbers were
approved by the maintainer on 2026-10-09.

**Status.** The perimeter settings and the realm lifespans are implemented
(#176) but not yet run on a cluster, and no timing has been measured on a
running stack. Logout, the native Superset and OpenMetadata sessions and the
recovery procedure are *designed*, not built. Where a number depends on upstream
behaviour that was not checked, the row says "to verify" and names the change
that must verify it. An integration that cannot meet a bound must document the
limitation and test it; "takes effect on next login" is not acceptable.

## Policy

| Setting | Value |
| --- | --- |
| Perimeter cookie | 30 min idle, 10 h absolute |
| Access token lifetime | 5 min |
| Issuer SSO session | 30 min idle, 10 h absolute, so it never outlives the perimeter cookie |
| Maximum role-revocation or account-disable delay, perimeter | 5 min |
| Maximum delay, native Superset and OpenMetadata sessions | 15 min |
| Logout | Platform-wide |

"Delay" is the time from the administrator's change in the issuer to the first
request that is refused. A user who holds no valid session when the change is
made is refused at the next login.

## Three kinds of session

| Session | Lives in | Ends when | Renewal |
| --- | --- | --- | --- |
| Issuer SSO session | Keycloak, `auth.<base_domain>` | 30 min idle or 10 h absolute, user disabled, or logout | None; the user logs in again |
| Perimeter cookie | oauth2-proxy cookie on `.<base_domain>` | 30 min idle, 10 h absolute, or a refresh the issuer refuses | While the cookie is valid the proxy refreshes the access token at the issuer before it is 5 min old; the refresh re-reads the user's groups |
| Native app session | Superset and OpenMetadata, behind the perimeter | 15 min after it was created, not sliding | The app sends the browser back through the issuer, which answers silently while the SSO session is valid, and the app re-reads the role claim |

Native sessions are short because the app, not the perimeter, evaluates them:
the perimeter cannot end a Superset session it did not create. Dagster, the
Trino UI and the portal have no native session, so the perimeter bound alone
applies.

### Why the bounds hold

| Bound | Mechanism | Verified |
| --- | --- | --- |
| Perimeter, 5 min | The proxy cannot serve a request on an expired access token, and a refresh for a disabled user or a user whose SSO session ended is refused. A removed role changes the groups in the next token. | **Read in the source of the pinned oauth2-proxy v7.15.5, not run.** The allowed groups are checked against the session on every request (`getAuthenticatedSession`, `ProviderData.Authorize`), so a refresh that changes the groups changes the decision. A refresh redeems the refresh token and replaces the session groups from the new ID token (`OIDCProvider.RefreshSession`). A refresh the issuer answers with `invalid_grant` or `invalid_client` clears the session; any other refresh error keeps it only until its access token (5 min) expires. **To verify on a cluster**: that Keycloak refuses the refresh for a disabled user and that the measured delay is within 5 min. |
| Native, 15 min | Superset: non-sliding session lifetime of 15 min with roles synced at login. OpenMetadata: its own session or token expiry set to 15 min, if the pinned version exposes one. | **To verify** in #25 against the pinned versions. If a version cannot meet 15 min, #25 records the limit and the exposure window in this file. Specified in [`identity-service-permissions.md`](identity-service-permissions.md): Superset by an absolute-age check, OpenMetadata by issuer-refreshed 5 min tokens (its HTTP session cannot be set below 1 h) with role changes bounded by `olf` rather than the session. |
| Faster offboarding | The administrator may also end the user's sessions in the issuer. This shortens the window but is not what the bound relies on. | Not verified. |

## Logout

Logout is platform-wide: one action ends the perimeter cookie, the issuer SSO
session, and the native sessions of the integrated services. The perimeter
sign-out redirects through each service's logout endpoint and then the
issuer's end-session endpoint. A native session that survives (a missed
endpoint, a cookie the browser kept) still dies within 15 min.

The risk this closes: on a shared browser, user B signs in after user A and
reaches a surviving native session of A. #25 and #176 must test repeated
logout and login as different users across every enabled service.

## Issuer outage: fail closed

The perimeter does not extend sessions it cannot refresh and has no stale
grace period. Existing sessions keep working until the access token expires,
at most 5 min, then every protected route refuses requests. New logins fail.
Native sessions end by their own 15 min expiry and cannot be renewed. The
login route is not behind the perimeter and shows the issuer's unavailability;
the portal is behind it (#176), so it refuses too. This is the lock-out the
recovery below exists for.

## What each integration must do

| Integration | Required | Change |
| --- | --- | --- |
| oauth2-proxy (#176) | Refresh against the issuer at most every 4 min; cookie 30 min idle and 10 h absolute; no stale-session grace; sign-out chained to service logouts and the issuer | #176. Set: `--cookie-refresh=4m`, `--cookie-expire=30m`; the 10 h bound is the realm's SSO maximum ending the refresh chain. The sign-out chain is not built |
| Superset (#25) | OIDC login; 15 min non-sliding session; roles synced at login from `shared.identity.roles`; logout endpoint reachable from the chain | #25 |
| OpenMetadata (#25) | OIDC login; session or token expiry 15 min renewing through the issuer; logout in the chain | #25, expiry setting to verify |
| Dagster, Trino UI | Perimeter only; no native session | #176 (implemented) |
| Keycloak realm | Access token lifespan 5 min; SSO session idle 30 min, max 10 h | Set in the realm file (`accessTokenLifespan` 300, `ssoSessionIdleTimeout` 1800, `ssoSessionMaxLifespan` 36000). Not yet read back from a running Keycloak |

oauth2-proxy keeps a session's roles until the next sign-in whenever the
issuer's refresh response omits the ID token (optional in OIDC Core 12.2).
Keycloak returns one (to verify on the running stack), so the 5 min bound
holds there. An external issuer must return an ID token with the role claim on
refresh, or a removed role stays usable until its SSO session ends (at most
10 h).

## User lifecycle with `olf users`

User records live in the issuer. Only the first row, onboarding the profile's
`bootstrap_admins`, happens during `olf deploy`, which has no `--json` option;
every later step is an `olf users` command that needs no deploy. Every
`olf users` command takes `--json`, prints no password, token or setup link,
and is declared in ADR 0014 seam 4.

| Step | Command | What happens |
| --- | --- | --- |
| First administrators | `spec.identity.bootstrap_admins` in the profile, then `olf deploy` | Each address that has no account is invited as `platform-admin`. An account in any state is left alone, so a redeploy never resends mail, resets a password, re-enables a disabled user or restores a revoked role; removing an entry deletes nothing. Needs SMTP: without it the address is reported, nothing is created and the platform stays protected |
| Onboard | `olf users invite <email> --role <role>` | A pending account holding the role, and a setup email whose link lasts 24 hours. The member follows it, confirms the address, chooses their own password (and, because the realm requires one, a first and last name) and then signs in |
| Link expired or lost | `olf users resend <email>` | A new link for a pending account only; an active or disabled account is refused, so resend adds no access |
| Withdraw | `olf users cancel <email>` | Deletes a pending account; its mailed links stop working. An active or disabled account is refused |
| Mail failed | the command exits 2 and says so | The account is pending and has no access (it has no password). Fix the relay and run `resend`; no deploy. For bootstrap administrators the account is removed instead, so the next `olf deploy` retries |
| Change role | `olf users grant` / `revoke` | Group membership changes at once; the user's next token carries the new roles. A role-less user is authenticated and denied everywhere (`olf users list` flags them) |
| Offboard | `olf users disable <email>` | Disables the account and ends its issuer sessions; a disabled user cannot sign in again. `enable` reverses it. Revoke the role as well if the account may be re-enabled later: a redeploy does not restore either |
| Forgotten password | the sign-in page's reset link | Needs SMTP; the reset link is mailed by the issuer. No temporary password is ever distributed |

What is verified: `olf e2e run --env local` invites throwaway members through
a test mail sink, follows the mailed link as the new member and signs in with
the invited role claim; shows a pending or undelivered account cannot sign in
and a cancelled one's link leads nowhere; shows resend and cancel refuse active
accounts; revokes and
disables users and signs in again to see the changed claim and the refused
login; and runs the bootstrap step twice around an offboarding to show it is
not undone.

What is not: how long an *already open* perimeter or native session survives
`revoke` or `disable` is the bound in the policy table above, not a measurement.
Keycloak does not invalidate a setup link when it is used or when a later link
is mailed: until its 24 hours end, a link can open the password form again for
that account (observed by hand, not asserted). `cancel`, which deletes the
account, is the only early revocation. Credential recovery of an SSO account
belongs to the upstream issuer, and SSO account linking (#373) is not built.

## Cluster-administrator recovery (break-glass)

Designed, not implemented, not drilled. The drill ("Keycloak-outage recovery
drill") is an acceptance criterion of #177.

### Principles

- The authority is the Kubernetes API, not Keycloak, so it works with Keycloak
  down. The local operator is trusted, as #177 states.
- Recovery restores the issuer. It does not turn the perimeter off, add a
  public route or leave a standing credential.
- Nothing persistent grants access, so nothing needs removing except what the
  procedure itself records.

### Access path

| Item | Design |
| --- | --- |
| Required RBAC | A namespaced Role, bound to the recovering operator only: `get`/`list` on pods, services and events; `create` on `pods/portforward`; `get` on `pods/log`; `delete` on pods (to restart Keycloak). No Secret read and no `exec`. On the local kind cluster the kubeconfig is cluster-admin, so the Role matters for a non-admin operator and for #46. |
| Enabling | `kubectl port-forward` to the Keycloak Service in `olf-system`, bound to `127.0.0.1`. Dagster and the Trino UI are reached the same way, directly on their internal Services, because they have no native authentication to bypass. |
| Keycloak admin sign-in | The master-realm `admin` account, whose password is in `keycloak-admin-creds` and is not a realm user, so it works when realm login does not. If the Secret is lost, Keycloak's `bootstrap-admin` command creates a temporary admin from a one-off pod (**to verify** against the pinned Keycloak 26.6.4). |
| Expiry | The port-forward ends with its process; the operator-side wrapper takes a TTL (default 30 min) and exits at the end of it. There is no object to remove for access itself. |
| Credential rotation | Any use that revealed `keycloak-admin-creds` is followed by the admin rotation in [`identity-secret-rotation.md`](../setup/identity-secret-rotation.md#the-bootstrap-admin). A temporary admin from `bootstrap-admin` is deleted once the permanent one is restored. |
| Superset and OpenMetadata UIs | Not reachable during a Keycloak outage by design: they only accept issuer login. Administrative work on them during an outage goes through their CLI inside the pod. **Open question** for the maintainer: accept that, or require a local-admin fallback in each. |

### Observability without Grafana

| Signal | Where |
| --- | --- |
| Recovery in progress | The wrapper creates a labelled ConfigMap (`openlakeforge.io/break-glass`) in `olf-system` recording service, start, TTL and the kubeconfig user, and deletes it on exit. One left behind means an unclean exit and is itself the alert. `olf diagnostics collect` includes it. |
| Keycloak admin sign-in | Keycloak writes admin events and logins to its logs: `kubectl logs`. |
| API-level proof | Kubernetes audit logging. kind does not enable it by default; the follow-up decides whether to add it to the kind config. Until then the ConfigMap's user is advisory, because a claimed username is not proof of identity. |

### Public perimeter stays closed

The procedure edits no `Ingress`, middleware or route, so a drill must show,
before and after, that `kubectl get ingress -A` is unchanged and that an
unauthenticated request to a protected public route is still refused.

## Not claimed

No timing in the policy table, no Keycloak, Superset or OpenMetadata behaviour,
and no part of the recovery has been run. The oauth2-proxy rows above were read
from the pinned source. `olf e2e run --env local` exercises the perimeter
(unauthenticated redirect and 401, per-role allow and deny, forged identity
headers, a direct API call with a session) but not revocation timing, an
issuer outage with an open session, logout or latency. The verifying changes
are named in each row.
