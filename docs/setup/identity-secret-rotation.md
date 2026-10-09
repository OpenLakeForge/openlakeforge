# Rotating an identity client secret

Each OIDC client of the local Keycloak adapter (`perimeter`, `superset`,
`openmetadata`, `trino`) authenticates to the issuer with a secret that
Terraform generates into a Kubernetes Secret (ADR 0014). This procedure
replaces one of them. It applies to the local and on-premises Keycloak
adapter; with `spec.identity.issuer: external` the issuer and the Secrets
belong to the operator, who rotates them in the issuer and in the Secrets
named by the contract (`oidc-client-<consumer>`, key `client-secret`).

Terraform state holds the generated values next to the Secrets (see
`docs/technical-debt.md`), so rotation also replaces the copy in state.

## When

Rotate after a suspected leak, when someone who could read the Secret leaves,
or on a schedule you set. A rotation has no downtime for logins in progress;
a service that has already loaded the old value fails its next token request
until it restarts.

## Procedure

1. Name the client. The Terraform address is
   `module.keycloak[0].random_password.client["<consumer>"]`.

2. Re-run the platform phase with that resource replaced. Terraform reads
   extra arguments from `TF_CLI_ARGS_apply`; the escaped quotes are needed
   because Terraform splits the value like a shell:

   ```bash
   TF_CLI_ARGS_apply='-replace=module.keycloak[0].random_password.client[\"perimeter\"]' \
     uv run --project tools/olf --locked olf deploy --provider local --phase platform
   ```

   The apply replaces the password, writes the new value to
   `keycloak-client-<consumer>`, and replaces the realm Job
   (`keycloak-realm-<revision>`), whose changed Secret value makes
   `keycloak-config-cli` update the client in Keycloak. Other clients keep
   their secrets.

3. Restart every workload that reads the Secret, so it loads the new value.
   Environment variables from a Secret are read at container start.
   Services that consume the clients (#25, #26, #176) are not wired yet; when
   they are, list their restart here.

4. Confirm. The old value must be refused and the new one accepted. The token
   endpoint answers `unauthorized_client` for a wrong secret and
   `invalid_grant` for a right secret with a bad code, so no login is needed:

   ```bash
   olf e2e run --env local   # "identity issuer and role claims" asserts the Secret authenticates each client
   ```

## Exercise

Performed on the local stack (kind, Keycloak 26.6.4) while closing #24:

| Step | Observed |
| --- | --- |
| Before: the `perimeter` Secret value at the token endpoint | `invalid_grant` (accepted) |
| `TF_CLI_ARGS_apply=... olf deploy --provider local --phase platform` | `Apply complete! Resources: 2 added, 1 changed, 2 destroyed`; the realm Job was replaced (`keycloak-realm-064161993b` to `keycloak-realm-7512c40f03`) |
| After: the old value | `unauthorized_client` (refused) |
| After: the Secret's new value | `invalid_grant` (accepted) |
| After: `superset` and `trino` with their unchanged Secrets | `invalid_grant` (still accepted) |

## The bootstrap admin

`keycloak-admin-creds` is used by Keycloak on its first start against an empty
database. Replacing its `random_password` updates the Secret and the realm
Job's login but does not change an admin that already exists; change the admin
password in the Keycloak console first, then update the Secret to match.
