# Bring your own OIDC issuer

How to sign people in through an identity provider you run, such as Okta,
Entra ID or Auth0, instead of GitHub. OpenLakeForge then deploys no issuer
of its own.

```yaml
spec:
  access:
    issuer: none
  identity:
    issuer: external
    issuer_url: https://login.example.com/
    role_claim: groups
    role_mapping:
      platform-admin: [olf-admins]
      analyst: [analysts]
```

| Field | Meaning |
| --- | --- |
| `issuer_url` | An `https://` URL. The token's `iss` claim must equal it. |
| `role_claim` | The token claim that holds group values, such as `groups`. |
| `role_mapping` | Each canonical role and the claim values that grant it. A value may not map to two roles. |
| `client_ids` | Optional issuer-assigned client ids, keyed by consumer. |

Every field is in the
[Deployment Profile reference](../../reference/deployment-profile.md#specidentity).

## Register the clients

Register four confidential OIDC clients in your issuer: `perimeter`,
`superset`, `openmetadata` and `trino`, or the ids you set under `client_ids`. Store each
client secret in a Kubernetes Secret named `oidc-client-<consumer>` in
`olf-system`, with the key `client-secret`. The contract names all four, so
create all four; only the `perimeter` client is used by a deployed service
today (see Limits).

For the `perimeter` client, register this redirect URI, with the scheme you
deploy (`http` with `access: {issuer: none}`, `https` otherwise):

```text
<scheme>://oauth2.<base_domain>/oauth2/callback
```

For example `http://oauth2.olf.localhost/oauth2/callback`.

`olf-system` does not exist on a fresh stack, and `olf` creates it early only
for `issuer: github`. Do not create it by hand: Terraform owns it and cannot
adopt a hand-made one. The order to use: run `olf deploy --provider local`,
which creates the namespace and may fail or wait on the pods that need the
Secrets; then create the Secrets below and run `olf deploy --provider local`
again. This order has not been run end to end; the missing early namespace step
for `external` is tracked in #521.

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
"$(olf toolchain path kubectl)" -n olf-system create secret generic oidc-client-perimeter \
  --from-literal=client-secret="$PERIMETER_CLIENT_SECRET"
```

Repeat for `superset`, `openmetadata` and `trino`.

Also register a fifth, public client for the command line: `olf-cli`, or the id
you set under `client_ids`. It has no secret and no Kubernetes Secret.

| Setting | Value |
| --- | --- |
| Client type | Public (no client secret) |
| Grant | Authorization Code with PKCE (S256) |
| Redirect URIs | Loopback addresses on any port (`http://127.0.0.1:<port>/...`, `http://localhost:<port>/...`, `http://[::1]:<port>/...`) |
| Scopes | `openid`, `email`, `profile` and `groups` (or whichever scope carries your `role_claim`), plus `offline_access` for refresh tokens |
| Claims | The `role_claim` and `preferred_username`: OpenMetadata names users by it |

The perimeter accepts a bearer token whose `iss` is your `issuer_url` and whose
`aud` is this client id, but only when your `role_claim` is `groups`: oauth2-proxy
reads the roles of a bearer token from that claim name alone, so with another
`role_claim` every bearer token is refused with 401. No `olf` command signs in with it yet.

## Limits

- **The issuer must be public and trusted.** The perimeter gets the local CA
  only when Dex is deployed. With an external issuer, the issuer must be
  reachable from inside the cluster and present a publicly trusted
  certificate; a private CA is not supported.
- **Refresh must return the role claim.** The perimeter re-reads roles when it
  refreshes a session. If your issuer's refresh response carries no ID token
  with the role claim, a removed role stays usable until the issuer's session
  ends, which can be hours. Test removal before you rely on it.
- **Only the perimeter uses your issuer.** With `external`, OpenMetadata and
  Superset are deployed with no OIDC configuration (`oidc = null`), so they
  keep their own logins behind the perimeter, and the Trino UI's OAuth2
  sign-in is not set up (it is Dex only). The OpenMetadata role sync, which
  reads GitHub teams, does not apply either. The per-service roles in
  [Roles and permissions](../../reference/roles-and-permissions.md) are
  therefore enforced by the perimeter only: which services a person opens.
- **Rotation is yours.** `olf secrets rotate` works for `issuer: github` only.
  Rotate the client secrets in your issuer and in the `oidc-client-<consumer>`
  Secrets, then restart the consumers.
- **Local provider.** Sign-in is implemented for the local provider only.

The mapping, roles and what they open are the same as with GitHub:
[Roles and permissions](../../reference/roles-and-permissions.md).
