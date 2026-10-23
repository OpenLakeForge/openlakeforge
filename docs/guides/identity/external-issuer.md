# Bring your own OIDC issuer

How to sign people in through an identity provider you run, such as Okta,
Entra ID or Keycloak, instead of GitHub. OpenLakeForge then deploys no issuer
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

Register four OIDC clients in your issuer: `perimeter`, `superset`,
`openmetadata` and `trino`, or the ids you set under `client_ids`. Store each
client secret in a Kubernetes Secret named `oidc-client-<consumer>` in
`olf-system`, with the key `client-secret`. The contract names all four, so
create all four; only the `perimeter` client is used by a deployed service
today (see Limits):

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
"$(olf toolchain path kubectl)" -n olf-system create secret generic oidc-client-perimeter \
  --from-literal=client-secret="$PERIMETER_CLIENT_SECRET"
```

Repeat for `superset`, `openmetadata` and `trino`.

## Limits

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
