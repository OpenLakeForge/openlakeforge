# Sign in with GitHub

This lesson turns sign-in on for the local deployment. People sign in with
GitHub, and the GitHub team they belong to decides which services they can
open.

**You will** add `spec.identity` to the profile, create and install a GitHub
App during `olf deploy`, and sign in as an analyst, in about 25 minutes.

**Start from** the project of [lesson 7](07-govern-and-trace-lineage.md): the
Full preset on a local deployment, so that Superset, Dagster and OpenMetadata
all run. You also need a GitHub organization where you can create teams and
install an app, and a GitHub account that is a member of it.

## How sign-in works

Without `spec.identity`, authentication is off and every service opens with
administrator rights. With it, a sign-in perimeter sits in front of every
service. The perimeter sends people to Dex, which asks GitHub who they are and
which of the mapped teams they belong to, and the highest role among those
teams is the role they get.

The four roles, highest first, are `platform-admin`, `data-engineer`,
`analyst` and `viewer`. The rights per service are in
[Roles and permissions](../reference/roles-and-permissions.md). Owning the
GitHub organization grants nothing: only members of a mapped team can sign in.

## 1. Create the teams

In your GitHub organization, create four teams. The slugs below are the ones
this lesson uses; any slugs work if the profile names them:

| Team | Role |
| --- | --- |
| `olf-platform-admins` | `platform-admin` |
| `olf-data-engineers` | `data-engineer` |
| `olf-analysts` | `analyst` |
| `olf-viewers` | `viewer` |

Add your own account to `olf-platform-admins` for now.

## 2. Add `spec.identity`

Keep the profile from the earlier lessons and add the `identity` block under
`spec`. Replace `my-org` with the organization login exactly as GitHub shows
it, case included:

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

`oauth_app_secret_ref` names the Kubernetes Secret that will hold the GitHub
App's credentials. You do not create it: the next step does. A role you leave
out of `teams` is held by nobody. Every field is in the
[Deployment Profile reference](../reference/deployment-profile.md#specidentity).

```bash
olf profile validate --project .
```

## 3. Deploy and connect GitHub

This lesson turns sign-in on for a fresh deployment. Start it clean:

```bash
olf destroy --provider local
olf deploy --provider local
```

Switching an existing authentication-off stack to GitHub sign-in in place is
not covered yet.

The GitHub step comes first, while you are still at the terminal:

1. `olf deploy` notices that the Secret `github-oauth-app` does not exist and
   prints a URL to confirm a GitHub App. The browser opens on it; on WSL the
   URL is opened through Windows when `explorer.exe` is available. Either way
   the URL is printed, so you can open it yourself.
2. In the browser, select **Create GitHub App**. The app is created in your
   organization, private to it, with read access to members and email
   addresses.
3. GitHub sends the browser back to `olf`, which stores the app's credentials
   in the Secret and sends the browser on to the app's install page.
4. On the install page, install the app on your organization.

`olf deploy` waits up to 15 minutes for step 2. After that it runs unattended
and no further input is needed. It only asks in an interactive terminal; in CI
it fails on a missing Secret instead, and `--no-github-connect` does the same
by choice. Credentials are never printed.

The deployment ends with the portal URL and the state of the app:

```text
GitHub App installed on my-org
```

If it prints `Install the GitHub App on my-org: <link>` instead, open the link
and install the app. Without the installation, nobody can sign in. To run the
GitHub step on its own later, see
[Connect GitHub](../guides/identity/connect-github.md).

## 4. Sign in on the portal

Open a private browser window on the portal:

```text
http://olf.localhost
```

You are sent to GitHub, then back through Dex to the portal, with no password
prompt from OpenLakeForge: GitHub owns your password and two-factor
authentication. The portal lists the services. As `platform-admin` you can open
all of them, and the portal links to the organization's teams page in GitHub,
where membership is managed.

> [!NOTE]
> Open a fresh private window for each sign-in below. A browser that keeps a
> session keeps the role it was signed in with until the session refreshes.

## 5. What each role sees

Move your account between teams in GitHub (organization, Teams) and sign in
again to see each role. The portal, Dagster, Trino UI and Superset follow at
sign-in. OpenMetadata roles come from a role sync that runs every 5 minutes,
not from sign-in: wait for it, or run it now:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
"$(olf toolchain path kubectl)" -n olf-system create job --from=cronjob/openmetadata-role-sync role-sync-now
```

Use a new job name each time you run it.

| Role | Superset | Dagster | Trino UI | OpenMetadata |
| --- | --- | --- | --- | --- |
| `platform-admin` | `Admin` | opens | opens | `Admin` |
| `data-engineer` | `Alpha` | opens | opens | `write` |
| `analyst` | `Gamma` | denied | denied | `read` |
| `viewer` | read-only | denied | denied | `read` |

A service the role does not open answers with an "Access is role-restricted"
page and a link back to the portal.

## 6. Plain HTTP or HTTPS

The default profile serves plain HTTP on `*.olf.localhost` (`issuer: none`).
It needs no certificate step, and it is allowed only on a `*.localhost`
domain, which is reachable from your machine alone. Sign-in works over it.

To serve HTTPS, declare `spec.access` without an issuer, so the platform uses
its own CA:

```yaml
spec:
  access: {}
```

Run `olf deploy --provider local` again, then trust the CA in your browser:

```bash
olf access trust
```

It writes the CA certificate and prints the commands for your platform. Skip
it and the browser warns on every host.

Trino's workload authentication follows `spec.access`: with the block
declared, as here, Superset, dbt and OpenMetadata ingestion authenticate to
Trino with their own passwords (`trino_workload_authentication` defaults to
`password`); without it, it defaults to `off`. The Trino UI sign-in follows
the scheme: on plain HTTP the UI opens as a fixed user behind the perimeter,
and on HTTPS it signs in through Dex.

The GitHub App records the callback URL for the scheme it was created with.
After switching scheme, run `olf secrets rotate github-app` to register an app
with the new URL, and install it again.

## 7. Authentication off

A profile with no `spec.identity` runs with authentication off: every service
opens with administrator rights and nobody signs in. `olf` allows that only
where nothing is published beyond your machine: on a `*.localhost` domain
locally, and on AWS or Azure without `spec.access`. It is what `olf init`
writes, and what lessons 1 to 7 used.

> [!WARNING]
> Once OpenMetadata has been deployed with sign-in on, removing
> `spec.identity` does not bring back its `admin@open-metadata.org` login.
> Destroy and recreate the deployment to go back.

## 8. Try the access model

This procedure uses one GitHub account that plays every role by moving between
teams. Your account must be in `olf-platform-admins` only at the start. Use a
fresh private window for each sign-in.

1. **The admin adds an analyst.** In GitHub, move your account from
   `olf-platform-admins` to `olf-analysts`. Note the time.
2. **The analyst signs in.** Open the portal (`http://olf.localhost`, or `https://olf.localhost` if you switched to HTTPS). You pass through
   GitHub and Dex to the portal, with no password prompt, and the portal lists
   the services.
3. **The analyst reaches Superset.** Open it from the portal. It opens signed
   in, with no login form. Open a dashboard and check it renders.
4. **The analyst is denied Dagster.** Open Dagster from the portal. The page
   answers 403 "Access is role-restricted", with a link back to the portal.
   The Trino UI is denied the same way.
5. **A user outside the teams is denied.** Remove your account from every
   OpenLakeForge team. If you own the organization, you stay an owner, and
   that is the point. Sign in from a fresh window. The sign-in is refused, with
   Dex's page explaining why, and no session exists.
6. **A removed member loses access.** Add your account back to
   `olf-analysts` and sign in. Then remove it from the team without signing
   out. At the first request at least 4 minutes after your sign-in or the last
   refresh, the perimeter re-reads your teams and refuses you. Note the time
   from removal to refusal; it stays within about 5 minutes.
7. **Automation is unaffected.** While you try the steps above,
   `olf e2e run --env local` still passes, and the OpenMetadata ingestion and
   dbt runs keep working. They use service credentials, not a person's
   session.

When you finish, put your account back in `olf-platform-admins`.

## Check it worked

This lesson ends on a person's access, so check it in the browser. Signed in
as an analyst, Superset opens and renders a dashboard, and Dagster answers
"Access is role-restricted". Signed in with an account that is in no mapped
team, GitHub's sign-in is followed by Dex's refusal page, which links to the
[troubleshooting steps](../guides/identity/connect-github.md#troubleshooting).

## What you learned

- `spec.identity` turns sign-in on; a GitHub team decides each person's role.
- `olf deploy` creates and stores the GitHub App for you, then you install it
  on the organization.
- Membership is managed in GitHub, and a removed member is refused within
  minutes, not at the next deploy.
- Plain HTTP is the default locally; `spec.access: {}` with `olf access trust`
  gives HTTPS.

## Next

[Add PROD and promote a revision](09-add-prod-and-promote.md). To manage
members, rotate secrets or recover when GitHub is unreachable, see the
[identity guides](../guides/identity/connect-github.md).
