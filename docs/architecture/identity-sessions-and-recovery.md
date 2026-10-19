# Identity session policy and administrator recovery

The session policy #176 and #25 implement (#177), and the design of the
cluster-administrator recovery that works when the issuer is down. Numbers were
approved by the maintainer on 2026-10-09.

**Status.** The perimeter settings and Dex's token lifetimes are implemented. The
revocation bound was measured once against real GitHub (#384, qualification run
1, below); no other timing has been measured on a running stack. Logout, the native Superset and OpenMetadata sessions and the
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
| Issuer session | Dex, `auth.<base_domain>` (refresh token) | 30 min unused or 10 h absolute, membership removed, or logout | None; the user logs in again |
| Perimeter cookie | oauth2-proxy cookie on `.<base_domain>` | 30 min idle, 10 h absolute, or a refresh the issuer refuses | While the cookie is valid the proxy refreshes the access token at the issuer before it is 5 min old; the refresh re-reads the user's groups |
| Native app session | Superset and OpenMetadata, behind the perimeter | 15 min after it was created, not sliding | The app sends the browser back through the issuer, which answers silently while the SSO session is valid, and the app re-reads the role claim |

Native sessions are short because the app, not the perimeter, evaluates them:
the perimeter cannot end a Superset session it did not create. Dagster, the
Trino UI and the portal have no native session, so the perimeter bound alone
applies.

### Why the bounds hold

| Bound | Mechanism | Verified |
| --- | --- | --- |
| Perimeter, 5 min | The proxy cannot serve a request on an expired access token, and a refresh for a disabled user or a user whose SSO session ended is refused. A removed role changes the groups in the next token. | **Read in the source of the pinned oauth2-proxy v7.15.5, not run.** The allowed groups are checked against the session on every request (`getAuthenticatedSession`, `ProviderData.Authorize`), so a refresh that changes the groups changes the decision. A refresh redeems the refresh token and replaces the session groups from the new ID token (`OIDCProvider.RefreshSession`). A refresh the issuer answers with `invalid_grant` or `invalid_client` clears the session; any other refresh error keeps it only until its access token (5 min) expires. **Measured on a cluster** (#384, run 1, real GitHub): a member moved to another team lost the old role on the first request after the 4 min `--cookie-refresh` (`Invalid authorization via session`, then 403). The bound is "the next request at least 4 min after sign-in or the last refresh", not a timer. |
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
| oauth2-proxy (#176) | Refresh against the issuer at most every 4 min; cookie 30 min idle and 10 h absolute; no stale-session grace; sign-out chained to service logouts and the issuer | #176. Set: `--cookie-refresh=4m`, `--cookie-expire=30m`; the 10 h bound is Dex's refresh-token absolute lifetime ending the refresh chain. The sign-out chain is not built |
| Superset (#25) | OIDC login; 15 min non-sliding session; roles synced at login from `shared.identity.roles`; logout endpoint reachable from the chain | #25 |
| OpenMetadata (#25) | OIDC login; session or token expiry 15 min renewing through the issuer; logout in the chain | #25, expiry setting to verify |
| Dagster, Trino UI | Perimeter only; no native session | #176 (implemented) |
| Dex | ID token 5 min; refresh token unused for 30 min, 10 h absolute | Set in the Dex config (`expiry.idTokens` 5m, `refreshTokens.validIfNotUsedFor` 30m, `absoluteLifetime` 10h; `modules/identity/dex/main.tf`) |

oauth2-proxy keeps a session's roles until the next sign-in whenever the
issuer's refresh response omits the ID token (optional in OIDC Core 12.2).
Dex returns one on a refresh (the measured run above depends on it), so the
5 min bound holds there. An external issuer must return an ID token with the role claim on
refresh, or a removed role stays usable until its SSO session ends (at most
10 h).

## User lifecycle in GitHub

People and their roles live in the client's GitHub organisation
([ADR 0018](../adr/0018-github-is-the-forge.md)): each canonical role maps to
one or more teams in `spec.identity.github` (ADR 0014, "The GitHub adapter
through Dex"). There is no `olf` command for users and no step needs a deploy.

| Step | In GitHub | What happens |
| --- | --- | --- |
| First administrators | members of the team mapped to `platform-admin` | Hold `platform-admin` at their first sign-in. Nothing is created at deploy |
| Onboard | add the person to the organisation and to a mapped team | They sign in with GitHub on the portal. GitHub owns their password, 2FA and recovery |
| Change role | move them between mapped teams | Their next token carries the new teams |
| Revoke every role | remove them from every mapped team | Dex refuses their next sign-in or refresh |
| Offboard | remove them from the organisation | Same, and GitHub ends their organisation access everywhere else at once |

The revocation bound above is the one measured in the qualification run:
Dex re-reads team membership on refresh, and the perimeter refreshes at most
every 4 min.

## Cluster-administrator recovery (break-glass)

Designed, not implemented, not drilled. The drill ("issuer-outage recovery
drill") is an acceptance criterion of #177. It was written against Keycloak;
Dex has no admin console or admin account, so the rows below that depended on
one are dropped and the rest stand.

### Principles

- The authority is the Kubernetes API, not the issuer, so it works with the
  issuer down. The local operator is trusted, as #177 states.
- Recovery restores the issuer. It does not turn the perimeter off, add a
  public route or leave a standing credential.
- Nothing persistent grants access, so nothing needs removing except what the
  procedure itself records.

### Access path

| Item | Design |
| --- | --- |
| Required RBAC | A namespaced Role, bound to the recovering operator only: `get`/`list` on pods, services and events; `create` on `pods/portforward`; `get` on `pods/log`; `delete` on pods (to restart Dex). No Secret read and no `exec`. On the local kind cluster the kubeconfig is cluster-admin, so the Role matters for a non-admin operator and for #46. |
| Enabling | `kubectl port-forward` to the Dex Service in `olf-system`, bound to `127.0.0.1`. Dagster and the Trino UI are reached the same way, directly on their internal Services, because they have no native authentication to bypass. |
| Expiry | The port-forward ends with its process; the operator-side wrapper takes a TTL (default 30 min) and exits at the end of it. There is no object to remove for access itself. |
| Superset and OpenMetadata UIs | Not reachable during an issuer outage by design: they only accept issuer login. Administrative work on them during an outage goes through their CLI inside the pod. **Open question** for the maintainer: accept that, or require a local-admin fallback in each. |

### Observability without Grafana

| Signal | Where |
| --- | --- |
| Recovery in progress | The wrapper creates a labelled ConfigMap (`openlakeforge.io/break-glass`) in `olf-system` recording service, start, TTL and the kubeconfig user, and deletes it on exit. One left behind means an unclean exit and is itself the alert. `olf diagnostics collect` includes it. |
| Dex sign-ins | Dex logs each login and refresh: `kubectl logs`. |
| API-level proof | Kubernetes audit logging. kind does not enable it by default; the follow-up decides whether to add it to the kind config. Until then the ConfigMap's user is advisory, because a claimed username is not proof of identity. |

### Public perimeter stays closed

The procedure edits no `Ingress`, middleware or route, so a drill must show,
before and after, that `kubectl get ingress -A` is unchanged and that an
unauthenticated request to a protected public route is still refused.

## Not claimed

No timing in the policy table beyond the one measurement above, no Superset or
OpenMetadata behaviour, and no part of the recovery has been run. The oauth2-proxy rows above were read
from the pinned source. `olf e2e run --env local` exercises the perimeter
(unauthenticated redirect and 401, per-role allow and deny, forged identity
headers, a direct API call with a session) but not revocation timing, an
issuer outage with an open session, logout or latency. The verifying changes
are named in each row.
