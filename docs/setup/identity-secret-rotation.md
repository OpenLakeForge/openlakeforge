# Rotating an identity client secret

Each OIDC client of the local Keycloak adapter (`perimeter`, `superset`,
`openmetadata`, `trino`) authenticates to the issuer with a secret that an
in-cluster bootstrap Job creates once, only if the Secret is missing (ADR 0014,
[`identity-credentials.md`](../architecture/identity-credentials.md)). Terraform
never holds the value: it knows the Secret name and key, and the consumers read
the value from the Secret at runtime. A redeploy reuses the Secrets; it is not a
rotation. This procedure replaces one. It applies to the local and on-premises
Keycloak adapter; with `spec.identity.issuer: external` the issuer and the
Secrets belong to the operator, who rotates them in the issuer and in the
Secrets named by the contract (`oidc-client-<consumer>`, key `client-secret`).

## When

Rotate after a suspected leak, when someone who could read the Secret leaves,
or on a schedule you set. A rotation has no downtime for logins in progress;
a service that has already loaded the old value fails its next token request
until it restarts.

## Procedure

1. Keep the value being retired, to test it afterwards:

   ```bash
   OLD=$(kubectl -n olf-system get secret keycloak-client-perimeter -o jsonpath='{.data.client-secret}' | base64 -d)
   ```

2. Delete the Secret. The bootstrap Job never overwrites an existing one:

   ```bash
   kubectl -n olf-system delete secret keycloak-client-perimeter
   ```

3. Re-run the platform phase with the bootstrap Job replaced. The Job
   (`keycloak-credentials-<revision>`) recreates every missing Secret and
   leaves the others alone; replacing it also replaces the realm Job
   (`keycloak-realm-<revision>`), so `keycloak-config-cli` pushes the new value
   into the client. Terraform reads extra arguments from `TF_CLI_ARGS_apply`:

   ```bash
   TF_CLI_ARGS_apply='-replace=module.keycloak[0].kubernetes_job_v1.credentials' \
     uv run --project tools/olf --locked olf deploy --provider local --phase platform
   ```

4. Restart every workload that reads the Secret, so it loads the new value.
   Environment variables from a Secret are read at container start.
   Services that consume the clients (#25, #26, #176) are not wired yet; when
   they are, list their restart here.

5. Confirm. The old value must be refused and the new one accepted. The token
   endpoint answers `unauthorized_client` for a wrong secret and
   `invalid_grant` for a right secret with a bad code, so no login is needed.
   Test the retired value itself, then the new one:

   ```bash
   NEW=$(kubectl -n olf-system get secret keycloak-client-perimeter -o jsonpath='{.data.client-secret}' | base64 -d)
   for s in "$OLD" "$NEW"; do
     curl -sk -d grant_type=authorization_code -d code=invalid -d client_id=perimeter \
       -d redirect_uri=https://app.olf.localhost/oauth2/callback --data-urlencode "client_secret=$s" \
       https://auth.olf.localhost/realms/openlakeforge/protocol/openid-connect/token; echo
   done   # first: unauthorized_client, second: invalid_grant
   ```

   (Use the issuer URL and client id from the identity contract; `olf e2e run
   --env local` also asserts that each Secret authenticates its client.)

## Failure recovery

- The bootstrap Job fails with `Secret <name> exists without key <key>` when
  something else created that Secret. It changes nothing; fix or delete the
  Secret and re-run step 3.
- An interrupted bootstrap is safe to re-run: each Secret is created in one
  call, so no half-written Secret exists, and Secrets already created are kept.
- If the apply stops after step 3 recreated the Secret but before the realm Job
  ran, Keycloak still holds the old value. Re-run step 3; the realm Job applies
  the Secret.

## Exercise

Performed on the local stack (kind, Keycloak 26.6.4) while landing the
bootstrap Job, for the `perimeter` client, following steps 2, 3 and 5:

| Step | Observed |
| --- | --- |
| Delete `keycloak-client-perimeter`, then `TF_CLI_ARGS_apply='-replace=module.keycloak[0].kubernetes_job_v1.credentials' olf deploy --provider local --phase platform` | `Apply complete! Resources: 2 added, 0 changed, 2 destroyed` (the bootstrap Job and the realm Job); the Job log shows `Created Secret keycloak-client-perimeter` and `exists; keeping it` for the other four |
| After: the old value at the token endpoint | `unauthorized_client` (refused) |
| After: the Secret's new value | `invalid_grant` (accepted) |
| A foreign `keycloak-client-trino` Secret without `client-secret` | The Job fails with `exists without key client-secret; refusing to modify it` and leaves the Secret as it was |
| `keycloak-admin-creds` deleted while Keycloak is deployed | The Job fails with the pointer to the admin recovery and creates nothing |
| `olf e2e run --env local` after the drill | Passes, including each client's Secret authenticating it |

Step 4 (restarting consumers) was not exercised: no consumer is wired yet.

## The bootstrap admin

`keycloak-admin-creds` (keys `username`, `password`) is created by the same Job
and survives switching `spec.identity.issuer` to `external` and back. Keycloak
reads it on its first start against an empty database and ignores a changed
Secret once the admin exists; the realm Job authenticates with it on every run.
Deleting the Secret and re-running the Job is therefore not a rotation: while
the Keycloak Deployment exists the Job refuses to create a new value, because
it would not match the stored admin. To rotate it (unverified):

1. Sign in to the Keycloak console as `admin` and set a new password (master
   realm, Users, admin, Credentials).
2. Update the Secret to that value, then re-run the platform phase so the realm
   Job proves it authenticates:

   ```bash
   kubectl -n olf-system create secret generic keycloak-admin-creds \
     --from-literal=username=admin --from-literal=password='<new>' \
     --dry-run=client -o yaml | kubectl -n olf-system apply -f -
   ```
