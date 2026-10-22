# Authentication and permissions

How to turn on sign-in for a local OpenLakeForge deployment, map GitHub teams
to the four OpenLakeForge roles, see what each role may open, and rotate the
secrets sign-in depends on.

Without `spec.identity` in `openlakeforge.yaml`, authentication is off and
every service opens with administrator rights. `olf` allows that only on the
local provider with a `*.localhost` domain. With `spec.identity`, every
service sits behind a sign-in perimeter, and each person gets the role their
GitHub team grants.

> [!NOTE]
> Sign-in is implemented for the local provider. The AWS and Azure
> deployments publish no routes yet, so they are reached with `olf forward`
> and never ask anyone to sign in.

## Roles

People hold one of four canonical roles, highest first. A role decides which
services a person can open and the role they get inside each service:

| Service | `platform-admin` | `data-engineer` | `analyst` | `viewer` |
| --- | --- | --- | --- | --- |
| Portal | opens | opens | opens | opens |
| Dagster | full | full | denied | denied |
| Trino UI | opens | opens | denied | denied |
| Superset | `Admin` | `Alpha` | `Gamma` | read-only |
| OpenMetadata | `Admin` | `write` | `read` | `read` |

A grant applies on every enabled stage. A person refused a service sees an
"Access is role-restricted" page. Membership is managed in GitHub: adding
someone to a mapped team gives them the role, and removing them takes it away.
OpenLakeForge roles are never cloud IAM permissions.

## Turn on sign-in with GitHub

1. Add an identity block to `openlakeforge.yaml`. Each role maps to one or
   more team slugs, as they appear in the team URL; a role you leave out is
   held by nobody:

   ```yaml
   spec:
     access:
       issuer: none
     identity:
       issuer: github
       github:
         organization: my-org
         teams:
           platform-admin: [olf-platform-admins]
           data-engineer: [olf-data-engineers]
           analyst: [olf-analysts]
           viewer: [olf-viewers]
         oauth_app_secret_ref:
           name: github-oauth-app
   ```

   `organization` is the login exactly as GitHub shows it, case included.
   Only members of the mapped teams can sign in; organization ownership grants
   nothing. Sign-in works over plain HTTP or HTTPS.

2. Deploy:

   ```bash
   olf deploy --provider local
   ```

   In an interactive terminal, `olf deploy` notices the missing GitHub
   credentials and offers to create them: it opens GitHub in your browser,
   where you select **Create GitHub App** with your own session, and stores the
   app's credentials in the Kubernetes Secret named by `oauth_app_secret_ref`.
   You can also run this step on its own, once the cluster exists:

   ```bash
   olf github connect --project .
   ```

3. Install the app on the organization when GitHub shows the install page.
   The app reads team membership only in organizations it is installed on.
   The deploy ends with the portal URL and either "GitHub App installed on
   <org>" or the install link.

`--no-browser` prints the URL instead of opening it; without a click within
15 minutes, nothing is written. Credentials are never printed. Running
`olf github connect` again registers a new app and replaces the Secret: delete
the old app and its private key in the organization settings yourself.

### Use an OAuth App instead

When you cannot create a GitHub App, create an organization-owned OAuth App
(Settings, Developer settings, OAuth Apps, New OAuth App):

| Field | Value |
| --- | --- |
| Homepage URL | `https://olf.localhost`, or `http://olf.localhost` with `issuer: none` |
| Authorization callback URL | `https://auth.olf.localhost/callback`, or `http://auth.olf.localhost/callback` with `issuer: none` |

Replace `olf.localhost` with your `spec.access.base_domain` if you changed it.
Generate a client secret, then store both values. The first `olf deploy`
creates the `olf-system` namespace and stops with the exact command; run it
with the managed kubectl:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
"$(olf toolchain path kubectl)" -n olf-system create secret generic github-oauth-app \
  --from-literal=client-id="$GITHUB_CLIENT_ID" \
  --from-literal=client-secret="$GITHUB_CLIENT_SECRET"
```

Then run `olf deploy --provider local` again, with `--no-github-connect` so it
does not offer to create a GitHub App.

## Use another OIDC issuer

`issuer: external` uses an identity provider you run, such as Okta, Entra ID or
Keycloak. OpenLakeForge then deploys no issuer of its own:

```yaml
spec:
  identity:
    issuer: external
    issuer_url: https://login.example.com/
    role_claim: groups
    role_mapping:
      platform-admin: [olf-admins]
      analyst: [analysts]
```

`role_claim` is the token claim that holds group values, and `role_mapping`
maps each role to the values that grant it. You register four OIDC clients in
your issuer (`perimeter`, `superset`, `openmetadata` and `trino`, or the ids
you set under `client_ids`) and store each client secret in a Secret
`oidc-client-<consumer>` with the key `client-secret`. Every field is in the
[Deployment Profile reference](../reference/deployment-profile.md#specidentity).

## Rotate secrets

Rotate after a suspected leak, when someone who could read the Secrets leaves,
or on a schedule. With `issuer: external`, rotate the client secrets in your
issuer and in the `oidc-client-<consumer>` Secrets, then restart the
consumers. The steps below are for `issuer: github`. They follow the
deployed Jobs but have not been exercised as a drill yet.

The commands use the managed kubectl and kubeconfig:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
KUBECTL="$(olf toolchain path kubectl)"
```

**The GitHub app's secret.** Generate a new client secret in GitHub, update
the Secret named by `oauth_app_secret_ref`, and restart Dex:

```bash
"$KUBECTL" -n olf-system rollout restart deployment dex
```

**An OIDC client secret.** Dex holds one secret per client (`perimeter`,
`superset`, `openmetadata`, `trino`), created once by a bootstrap Job that
never overwrites an existing Secret. To replace one, for example `perimeter`:

1. Delete it:

   ```bash
   "$KUBECTL" -n olf-system delete secret dex-client-perimeter
   ```

2. Re-run the platform phase with the bootstrap Job replaced, so it recreates
   the missing Secret:

   ```bash
   TF_CLI_ARGS_apply='-replace=module.dex[0].kubernetes_job_v1.credentials' \
     olf deploy --provider local --phase platform
   ```

3. Restart Dex and the workloads that read the secret:

   ```bash
   "$KUBECTL" -n olf-system rollout restart deployment dex
   "$KUBECTL" -n olf-system rollout restart deployment -l openlakeforge.io/component=perimeter
   ```

   For `trino`, also restart the `trino-coordinator` Deployment. For
   `superset`, each stage keeps its own copy, made by the one-shot
   `kubernetes_job_v1.oidc_secret` of that stage's `module.superset`: add a
   `-replace` of that Job for each stage in step 2, then restart Superset.

4. Sign in through the portal: it succeeds only when Dex and the service hold
   the same secret.

**The session cookie secret.** Rotating it signs everyone out:

```bash
"$KUBECTL" -n olf-system delete secret oauth2-proxy-cookie --ignore-not-found
TF_CLI_ARGS_apply='-replace=module.perimeter.kubernetes_job_v1.cookie_secret' \
  olf deploy --provider local --phase platform
"$KUBECTL" -n olf-system rollout restart deployment -l openlakeforge.io/role-floor
```

If the bootstrap Job fails with `Secret <name> exists without key
client-secret`, something else created that Secret: fix or delete it and
re-run step 2. An interrupted bootstrap is safe to re-run.

## Troubleshoot sign-in

| Symptom | Cause |
| --- | --- |
| "Sign-in was refused" | The GitHub App is not installed on the organization, or you are in no mapped team. |
| "Access is role-restricted" | Your role does not open this service; see [Roles](#roles). A platform-admin changes team membership in GitHub. |
| `olf deploy` stops on a missing Secret | Run `olf github connect --project .`, or create the OAuth App Secret as above. |
| A request from `curl` answers `302` or `401` | Every route except `auth.<base_domain>` requires a browser session; `olf`'s own commands port-forward instead. |

## Check it worked

Open the portal, `http://olf.localhost`. It sends you to sign in with GitHub,
then shows the portal. With the Full preset, a person in the `analyst` team
reaches Superset and is refused Dagster.
