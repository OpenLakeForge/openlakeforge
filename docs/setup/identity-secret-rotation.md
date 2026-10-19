# Rotating an identity client secret

Each OIDC client of the local Dex adapter (`perimeter`, `superset`,
`openmetadata`, `trino`) authenticates to the issuer with a secret that an
in-cluster bootstrap Job creates once, only if the Secret is missing (ADR 0014,
[`identity-credentials.md`](../architecture/identity-credentials.md)). Terraform
never holds the value: it knows the Secret name and key, and the consumers read
the value from the Secret at runtime. A redeploy reuses the Secrets; it is not a
rotation. This procedure replaces one. It applies to the local and on-premises
Dex adapter (`spec.identity.issuer: github`); with `issuer: external` the issuer
and the Secrets belong to the operator, who rotates them in the issuer and in
the Secrets named by the contract (`oidc-client-<consumer>`, key
`client-secret`). The GitHub OAuth App's own client secret is rotated in GitHub
and in the Secret that `spec.identity.github.oauth_app_secret_ref` names
([`github-oauth-app.md`](github-oauth-app.md)), followed by a Dex restart.

## When

Rotate after a suspected leak, when someone who could read the Secret leaves,
or on a schedule you set. A service that has already loaded the old value fails
its next token request until it restarts.

## Procedure

Not yet exercised on a cluster since Dex replaced Keycloak; the steps follow
the Job and Deployment names in `modules/identity/dex`.

1. Delete the Secret. The bootstrap Job never overwrites an existing one:

   ```bash
   kubectl -n olf-system delete secret dex-client-perimeter
   ```

2. Re-run the platform phase with the bootstrap Job replaced. The Job
   (`dex-credentials-<revision>`) recreates every missing Secret and leaves the
   others alone. Terraform reads extra arguments from `TF_CLI_ARGS_apply`:

   ```bash
   TF_CLI_ARGS_apply='-replace=module.dex[0].kubernetes_job_v1.credentials' \
     uv run --project tools/olf --locked olf deploy --provider local --phase platform
   ```

3. Restart Dex and every workload that reads the Secret, so each loads the new
   value. Environment variables from a Secret are read at container start.
   Dex holds all four client secrets; the `perimeter` client is also read by
   the oauth2-proxy Deployments:

   ```bash
   kubectl -n olf-system rollout restart deployment dex
   kubectl -n olf-system rollout restart deployment -l openlakeforge.io/component=perimeter
   ```

   Superset does not read the shared Secret directly: each stage holds a copy
   made by the one-shot `oidc_secret` Job of its Superset module, which keeps
   the old value. To rotate `dex-client-superset`, also replace that Job in
   every stage (a `-replace` on `kubernetes_job_v1.oidc_secret` of each
   `module.superset`) before restarting Superset. OpenMetadata and Trino are not
   yet wired to their clients.

4. Confirm by signing in through the portal: the sign-in completes only when
   Dex and the consumer hold the same value.

## Failure recovery

- The bootstrap Job fails with `Secret <name> exists without key client-secret`
  when something else created that Secret. It changes nothing; fix or delete
  the Secret and re-run step 2.
- An interrupted bootstrap is safe to re-run: each Secret is created in one
  call, so no half-written Secret exists, and Secrets already created are kept.

## The perimeter cookie secret

`oauth2-proxy-cookie` signs and encrypts the perimeter session cookies. A
platform apply does not re-run its completed Job, so a deleted Secret stays
missing and the proxies cannot start. To recreate or rotate it (every browser
session ends; people sign in again):

```bash
kubectl -n olf-system delete secret oauth2-proxy-cookie --ignore-not-found
TF_CLI_ARGS_apply='-replace=module.perimeter.kubernetes_job_v1.cookie_secret' \
  olf deploy --provider local --phase platform
kubectl -n olf-system rollout restart deployment -l openlakeforge.io/role-floor
```

The Job log shows `Created Secret oauth2-proxy-cookie`. Not yet exercised as a
drill; the Job's create-if-missing behaviour is what the deploy ran.
