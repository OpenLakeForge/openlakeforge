# Connect GitHub

How to turn on GitHub sign-in for a local OpenLakeForge deployment: create the
GitHub App, install it on your organization, and fix a refused sign-in.

Without `spec.identity` in `openlakeforge.yaml`, authentication is off and
every service opens with administrator rights. `olf` allows that only when
nothing is published beyond your machine: locally on a `*.localhost` domain,
and on AWS or Azure without `spec.access`. With `spec.identity`, every
service sits behind a sign-in perimeter, and each person gets the role their
GitHub team grants. Lesson 8 of the tutorial walks through this once:
[Sign in with GitHub](../../tutorial/08-sign-in-with-github.md).

> [!NOTE]
> Sign-in is implemented for the local provider. The AWS and Azure
> deployments publish no routes yet, so they are reached with `olf forward`
> and never ask anyone to sign in.

## Turn on sign-in

1. Add an identity block to `openlakeforge.yaml`. Each role maps to one or
   more team slugs, as they appear in the team URL; a role you leave out is
   held by nobody. See [Map teams to roles](map-teams-to-roles.md).

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
   credentials and runs the GitHub step first. It prints a URL and opens it in
   your browser (on WSL through Windows' `explorer.exe`, when present), where
   you select **Create GitHub App** with your own session. It stores the app's
   credentials in the Kubernetes Secret named by `oauth_app_secret_ref`, and
   the rest of the deploy runs unattended.

   You can also run this step on its own, once the cluster exists:

   ```bash
   olf github connect --project .
   ```

3. Install the app on the organization on the install page GitHub shows
   after you create it. The app reads team membership only in organizations
   it is installed on. The deploy ends with the portal URL and either
   "GitHub App installed on <org>" or the install link.

`--no-browser` (on `olf github connect`) prints the URL without opening it;
without a click within 15 minutes, nothing is written. Credentials are never
printed. To replace the app, use `olf secrets rotate github-app`, which
registers a new app, replaces the Secret and restarts Dex
([Rotate secrets](rotate-secrets.md#the-github-app)); then delete the old app
and its private key in the organization settings yourself. To make `olf deploy` fail on a missing Secret instead of
creating the app, pass `--no-github-connect`. In CI (no terminal, or `CI`
set) it never asks.

## Use an OAuth App instead

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
does not offer to create a GitHub App. An OAuth App has no installation step.

## Troubleshooting

Dex's refusal page ("Internal Server Error", with the two likely causes) and
the perimeter's "Sign-in was refused" page both link here.

| Symptom | Cause and fix |
| --- | --- |
| "Sign-in was refused" | The GitHub App is not installed on the organization, or you are in no mapped team. Check `olf deploy`'s last lines for the install link; add yourself to a mapped team in GitHub. |
| "Access is role-restricted" | Your role does not open this service; see [Roles and permissions](../../reference/roles-and-permissions.md). A platform-admin changes team membership in GitHub. |
| `olf deploy` stops on a missing Secret | Run `olf github connect --project .`, or create the OAuth App Secret as above. |
| The organization is spelled differently | `organization` is compared exactly, case included. Use the login as GitHub shows it. |
| A request from `curl` answers `302` or `401` | Every route except `auth.<base_domain>` requires a browser session; `olf`'s own commands port-forward instead. |

## Check it worked

Open the portal, `http://olf.localhost`. It sends you to sign in with GitHub,
then shows the portal. With the Full preset, a person in the `analyst` team
reaches Superset and is refused Dagster.
