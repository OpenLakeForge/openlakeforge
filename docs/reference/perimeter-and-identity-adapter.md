# Perimeter and identity adapter

How the sign-in perimeter and the GitHub issuer adapter are built on the local
root. The rules they implement are the decisions of
[ADR 0014](../adr/0014-identity-and-authorization.md), cited here by their
IDs (D1 to D12); this page records the mechanics behind them. Where the two
disagree, the ADR binds and this page is fixed.

What each role opens is in [Roles and permissions](roles-and-permissions.md);
the session bounds are in
[`identity-sessions-and-recovery.md`](../architecture/identity/identity-sessions-and-recovery.md);
the `identity.oidc` and `shared.access.perimeter` fields are in
[`provider-contracts.md`](../architecture/provider-contracts.md#sharedidentity).

## Adding an issuer adapter

A new issuer (Cognito or IAM Identity Center, Entra ID, GCP, generic OIDC) is
a checklist, not a schema change (D2):

1. Emit `implementation: identity.oidc` with the adapter in `adapter`.
2. Provide `issuer_url`, `role_claim`, and `role_mapping` keyed by canonical
   role, in the issuer's own claim values. Dex: `groups` holding
   `<organisation>:<team-slug>`. Entra ID: group object IDs. Cognito:
   `cognito:groups` as the claim.
3. Provide all four `clients`: `perimeter`, `superset` and `openmetadata` with
   `client_id` and `secret_ref{name,key}`, and the public `olf-cli` with
   `client_id` only (a `secret_ref` on it is rejected). Secret values never
   enter Terraform outputs, the provider contract, logs or CI. Secrets the
   adapter generates are created in-cluster by a bootstrap Job, not by
   Terraform, so they are not in Terraform state.
4. Declare `capabilities` (`groups_in_token`, `logout_endpoint`,
   `email_delivery`) honestly; absent means not declared.
5. Add a perimeter adapter only if the cloud has its own (#180).
6. Add a fixture beside `identity-oidc-dex.json`; the conformance test runs
   every fixture, a Cognito-style issuer among them, through the same
   consumer-facing validation.

With `spec.identity.issuer: external` the local root deploys no issuer: the
contract comes from the profile, and the operator provides the three client
Secrets under the names the contract references (`oidc-client-<consumer>`,
key `client-secret`). `client_ids` overrides the issuer-assigned client id per
consumer (default: the consumer name). How to register the clients, `olf-cli`
included, is in [Bring your own OIDC issuer](../guides/identity/external-issuer.md).

## The GitHub adapter through Dex

`spec.identity.issuer: github` deploys Dex with its GitHub connector
(`infra/terraform/modules/identity/dex`, D9). The profile says which teams hold
which canonical role:

```yaml
spec:
  identity:
    issuer: github
    github:
      organization: acme
      teams:                      # canonical role -> team slugs
        platform-admin: [data-platform]
        data-engineer: [data-eng]
        analyst: [analysts]
        viewer: [dashboards]
      oauth_app_secret_ref:       # Dex's upstream GitHub OAuth App or GitHub App
        name: github-oauth-app
        client_id_key: client-id          # default
        client_secret_key: client-secret  # default
      host: github.example.com    # optional: GitHub Enterprise Server; absent is github.com
```

| Piece | How |
| --- | --- |
| Upstream app | A GitHub OAuth App owned by the organisation with the one callback `https://auth.<base_domain>/callback`. The connector requests `user:email` (it reads the primary, possibly private, address) and `read:org` (teams); both appear on the authorize page and in an organisation's approval request. The operator stores its client id and secret in the Secret named by `oauth_app_secret_ref` (a Secret name and two distinct keys). This is Dex's upstream client, distinct from the four downstream consumer clients of `identity.oidc`. An organisation that restricts third-party access must approve the app |
| GitHub App instead | `olf infra github connect` (#420) creates a GitHub App from a manifest in the browser and stores `client-id`, `client-secret`, `app-id` and `private-key` in the `oauth_app_secret_ref` Secret. Dex's connector works with a GitHub App's client credentials: the user-to-server token ignores scopes, `/user/orgs` and `/user/teams` accept it with no extra permission, and `/orgs/{org}/members/{user}` and team visibility need the app's *Members: read* permission with the app installed on the organisation. The OpenMetadata role sync reads `app-id` and `private-key`. An interactive `olf infra deploy` with no Secret runs the connect flow itself before the foundation apply; CI, piped runs and `--no-github-connect` refuse instead. The manual OAuth App remains supported |
| Admission | The connector's `orgs` lists the organisation with every mapped team. A GitHub user in none of them is refused by Dex at login, before any token exists |
| Case | Team slugs are lowercased by `olf`, because GitHub slugs are always lowercase. The organisation is kept as written: GitHub preserves the case of a login and Dex compares it exactly, so `openlakeforge` for the `OpenLakeForge` organisation refuses every member (observed on Dex v2.46.0) |
| GitHub Enterprise | `host` is rendered as the connector's `hostName`, so Dex uses `https://<host>/login/oauth/*` and `https://<host>/api/v3/*`. Dex requires `rootCA` with `hostName`; it trusts the platform CA from the `olf-local-ca` ConfigMap. A GHE server behind a different private CA needs a CA input, not yet provided |
| E2E without GitHub | `olf infra e2e run` runs a stub GHE (`olf/infra/e2e/github_stub.py`, a throwaway pod with a local-CA certificate) when Dex's `hostName` is `olf-e2e-github.<shared namespace>.svc`, and drives the perimeter's role decisions through Dex's real connector with throwaway users per team, plus one in no mapped team who must be refused at login. Otherwise those checks skip |
| Groups claim | `teamNameField: slug`; the `groups` claim carries `<organisation>:<team-slug>` |
| `role_mapping` | Rendered by the root from `spec.identity.github`: each canonical role to `<organisation>:<slug>` of its teams, `role_claim: groups`. A role with no team is held by nobody |
| Scopes | Dex returns the `groups` claim only for the `groups` scope and a refresh token only for `offline_access`, so the perimeter, Superset and OpenMetadata request `openid email profile groups offline_access`. The perimeter alone also requests `federated:id audience:server:client_id:olf-cli` (module variable `login_audience`), so its ID token carries `aud` `olf-cli` and the connector id Polaris names the principal by ([ADR 0019](../adr/0019-data-plane-authorization-deferral.md)). An external issuer gets neither extra scope |
| Token lifetimes | `expiry.idTokens: 5m`, `expiry.refreshTokens.validIfNotUsedFor: 30m`, `expiry.refreshTokens.absoluteLifetime: 10h`, `reuseInterval: 3s` (D10). Asserted by the adapter's e2e, including parallel refreshes |
| Clients | `perimeter`, `superset`, `openmetadata` as static clients with secrets from the bootstrap Job (#181). `olf-cli` is a public static client (`public: true`, no secret, no `redirectURIs`): Dex then accepts loopback redirects (`127.0.0.1`, `localhost`, `[::1]`) on any port and issues refresh tokens without a secret. Dex does not enforce PKCE; the client must send it. `olf-cli` lists `perimeter` and `superset` in `trustedPeers` (module variable `trusted_peers`), so the perimeter's login and Superset's per-user data access (#968, token `aud` `[olf-cli, superset]`) may ask for a token whose audience is `olf-cli` |
| Login entry | The portal at the base domain is the front door. The perimeter (`--skip-provider-button`) sends an unauthenticated visit to Dex; with one connector and `alwaysShowLoginScreen` off, Dex goes straight to GitHub, and `skipApprovalScreen` removes its consent page. The person sees GitHub's authorize page once per app. Dex's own login page is never shown |
| Single sign-on | Dex keeps no browser session of its own; GitHub's session makes the next login silent. A native Superset or OpenMetadata login bounces through Dex to GitHub and back without a prompt while the GitHub session lasts (not yet verified on a cluster) |
| Logout | Sign-out ends the perimeter cookie and the native Superset and OpenMetadata sessions. The signed-out page links to GitHub's sign-out and states that on a shared browser the next sign-in reuses the GitHub account still signed in there (D10) |
| Storage | Dex's `postgres` storage on its own `dex` database in the platform PostgreSQL. Refresh tokens, the upstream GitHub token and the signing keys stay in a database with the same exposure class, instead of `dex.coreos.com` custom resources that the Secret inventory and the exposure scan do not cover. Dex needs no Kubernetes API access, so no RBAC exception |
| Network | Dex calls `github.com` (token exchange) and `api.github.com` (user and team lookups) at every login and refresh: flow F28 in `release/network-flows.yaml` |
| Revocation | Team membership is read at login and on each refresh; the perimeter refreshes at most every 4 min. Measured once against real GitHub (#384, qualification run 1): a member moved to another team lost the old role on the first request after the 4 min refresh point |
| Mail | None. Dex sends no mail and declares `email_delivery: false` |
| Break-glass | The cluster administrator's kubeconfig, the Superset and OpenMetadata in-pod CLIs, Dagster and Trino through their internal Services ([Break-glass access](../guides/identity/break-glass.md)). Dex has no admin account; the issuer-outage drill runs in the local e2e (#177, #520) |

A member of several mapped teams holds the union of their roles, which by the
monotonic rule is the highest one. Whether membership of a child team counts
for a mapped parent team is not verified, so map the teams people are direct
members of.

## The perimeter

`infra/terraform/modules/access/perimeter` runs oauth2-proxy behind Traefik
`forwardAuth` (D3, D7, D8).

| Piece | How |
| --- | --- |
| Role floor | Grants are monotonic, so what a service admits is a prefix of `precedence`, described by its lowest role. The module computes each protected route's floor from its service name (`portal` has none: the lowest role) |
| Instances | One oauth2-proxy Deployment per distinct floor, started with `--allowed-group` for the `role_mapping` claim values of every role at or above it. Today `oauth2-proxy-viewer` (portal, Superset, OpenMetadata, Trino `/v1` with people's access) and `oauth2-proxy-data-engineer` (Dagster, Trino UI). A new floor is a new instance without new code |
| Middleware | Traefik file-provider middlewares, because a `Middleware` CRD cannot be planned before the Traefik release installs it. `olf-strip-identity` deletes every inbound `X-Forwarded-*` / `X-Auth-Request-*` identity header; `olf-auth-<floor>` forwards to that floor's proxy and copies only the proxy's `X-Auth-Request-*` headers upstream. The proxies run with `--set-authorization-header=true`; `olf-auth-<lowest floor>-api` copies `Authorization` upstream and is attached only to the platform API's `/api` route |
| Ingress | One `Ingress` per namespace and floor (`openlakeforge-routes[-<floor>]`) carrying the middleware annotation; the namespace's wildcard certificate is issued from a single primary `Ingress` ([ADR 0013](../adr/0013-service-access-and-ingress.md)) |
| `query_api` | Trino's `/v1` on `trino-<stage>.<base_domain>` (`stage/<stage>/query_api`) is given the `viewer` floor through `path_refs` when people sign in with GitHub, while the Web UI keeps the `query` floor. It is not listed in `protected_routes` |
| Login callback | One host, `oauth2.<base_domain>/oauth2/*`, answered by the any-role instance and not behind the perimeter. The `perimeter` client has that single redirect URI, and the cookie on `.<base_domain>` carries the session to every route |
| Cookie secret | Created by an in-cluster Job only if missing (`oauth2-proxy-cookie`); Terraform never reads it. Rotation: delete the Secret, replace the Job, restart the proxies (signs everyone out once) |
| Client secret | `secretKeyRef` to `identity.oidc.clients.perimeter.secret_ref` |
| Issuer endpoints | With an `internal_url` (Dex) the proxy runs `--skip-oidc-discovery` with `--login-url=<issuer_url>/auth`, `--redeem-url=<internal_url>/token` and `--oidc-jwks-url=<internal_url>/keys`. An `external` issuer is discovered at its public URL |
| Bearer tokens | Every instance that accepts them starts with `--skip-jwt-bearer-tokens=true`. With an `internal_url` the proxy's own verifier accepts the `olf-cli` audience (`--oidc-extra-audience=olf-cli`) and reads roles from `--oidc-groups-claim`. For an `external` issuer whose `role_claim` is `groups` it starts with `--extra-jwt-issuers=<issuer_url>=<olf-cli client_id>`; oauth2-proxy v7.15.5 reads an extra issuer's roles from a hard-coded `groups` claim, so with any other claim neither flag is set and a bearer token gets 401 (D7) |
| Session | `--cookie-expire=30m`, re-issued on every refresh (Traefik `addAuthCookiesToResponse` carries the refreshed cookie), `--cookie-refresh=4m`. The 10 h absolute bound is the refresh token's absolute lifetime ending the refresh chain, not a cookie lifetime |

Behaviour, decided by the proxy before a request reaches a service:

| Request | Answer |
| --- | --- |
| No session, browser | 302 to the issuer's login; after login back to the original URL |
| No session, `Accept: application/json`, `X-Requested-With`, or path under `/api`, `/graphql`, `/v1` | 401, never an HTML login page |
| `Authorization: Bearer` token issued to `olf-cli`, role permitted | 2xx from the service |
| That token, role not permitted | 403 |
| Bearer token expired, with a tampered signature, or issued to another client (`superset`) | 401 on an API path |
| Session, role not permitted, or no recognised role | 403 with the "Access is role-restricted" page and a link to the portal. The proxy clears the session cookie, so the next page load passes silently through the issuer's SSO session |
| Issuer down | A session the proxy cannot refresh expires with its 5 min access token and is not extended; a new login fails |
| Proxy down | forwardAuth cannot reach it and Traefik answers an error, never the service |
| Forged `X-Forwarded-*` / `X-Auth-Request-*` | Deleted before forwardAuth; the decision is the proxy's, from its own cookie |

`olf` reaches protected routes through port-forwards
(`olf.infra.access.service_url`); only the perimeter and ingress e2e checks
use the public hostnames. `olf infra e2e run --env local` exercises the
perimeter (`e2e/_perimeter.py`): per route, an unauthenticated request is
redirected to the issuer (or gets 401 with `Accept: application/json`), also
with forged identity headers; signed in as per-team stub users for each mapped
role and one in no mapped team, each route answers as
`release/identity-roles.yaml` says, a denial is the 403 page with a portal
link, and a direct API call carrying only a denied user's session cookie is
refused too.

Measured on kind on a CI runner: a Superset dashboard load (3 charts) adds
8-13 ms median and 132-149 ms p95. The e2e fails above 100 ms added median;
p95 is reported only.

## OpenMetadata role sync

OpenMetadata cannot read roles from the token
([`identity-service-permissions.md`](../architecture/identity/identity-service-permissions.md)),
so a CronJob sets them from GitHub teams (D11).

| Piece | How |
| --- | --- |
| Credential | The GitHub App from `olf infra github connect`, read from the `oauth_app_secret_ref` Secret (`app-id`, `private-key`). The job mints a short-lived installation token per run and reads each mapped team's members with it (*Members: read*). A hand-made OAuth App has neither key and gets no sync |
| Where it runs | `openmetadata-role-sync`, a CronJob in `olf-system` on the pinned `openmetadata_ingestion` image, applied by `olf infra openmetadata deploy-metadata`. The script is mounted from a ConfigMap because that image has no `olf`; it imports the standard library only and carries a copy of `github_app.app_jwt`'s signing, using the image's `cryptography` or else the `openssl` CLI, and exits naming what is missing. The App Secret and the `olf-automation` token reach it as `secretKeyRef`s, so reading the Secret needs no RBAC |
| GitHub Enterprise | The job calls `https://<host>/api/v3` and trusts only the image's public CAs; a server behind a private CA is not supported yet |
| Child teams | GitHub's team-members API includes the members of child teams. The sync stops, changing nothing, when a mapped team has child teams. The precedence between roles is read from `release/identity-roles.yaml` and passed to the job |
| Reconciliation | For every non-bot OpenMetadata user except `admin` and `olf-automation`: `isAdmin` for `platform-admin`, the `DataSteward` role for `data-engineer`, neither otherwise, including a user in no mapped team. Other roles are left alone. A person who has not signed in yet has no user and starts at the baseline |
| Interval | Every 5 minutes (`*/5 * * * *`), inside the 15 min native-service bound; not yet measured on a cluster |
| Network | Flow F30 in `release/network-flows.yaml`, the job to `api.github.com` on 443; its calls to OpenMetadata ride F13 |

## Local cluster notes

`*.localhost` is loopback for some clients, so back-channel token and JWKS
calls need the pod to reach Traefik at `*.<base_domain>` and trust the local
CA; the local root does both (ADR 0013). curl resolves `*.localhost` to
loopback itself without asking DNS, while getaddrinfo-based clients get the
Traefik address from cluster DNS. Single sign-on across hosts needs a cookie
on `.<base_domain>`: curl (libpsl) and headless Chromium both accepted a
`Domain=.olf.localhost` cookie and sent it to a sibling host, and both
rejected `Domain=.localhost`. Firefox and Safari were not tested.

With `spec.access.tls: none` (development on `*.localhost`), Dex issues
`http://auth.<base_domain>`, oauth2-proxy sets `--cookie-secure=false`, and
its callback is `http://oauth2.<base_domain>/oauth2/callback`.
