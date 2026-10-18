# Sign in with GitHub (`issuer: github`)

With `spec.identity.issuer: github`, people and roles live in your GitHub
organisation and Dex signs them in ([ADR 0014](../adr/0014-identity-and-authorization.md)).
Before `olf deploy` can start the issuer it needs a GitHub app and a Secret
holding its credentials. `olf github connect` does both; the manual OAuth App
below is the fallback.

## Quick path: `olf github connect`

With the profile below in `openlakeforge.yaml` and the kind cluster up (run
`olf deploy --provider local` once; it creates the `olf-system` namespace and
stops asking for the Secret):

```bash
olf github connect --project .
```

It opens your browser on GitHub, where you click **Create GitHub App** with
your own session (SSO and 2FA as the organisation enforces), stores the app's
`client-id`, `client-secret` and `private-key` in the Secret named by
`oauth_app_secret_ref`, and opens the page to install the app on the
organisation. Install it: the app's *Members: read* permission only applies
to organisations it is installed on. Then re-run `olf deploy`. Credentials are
never printed. `--no-browser` prints the URL instead; after 5 minutes without
a click it exits with nothing written. Re-running registers a new
GitHub App and replaces the Secret; delete the previous app and its private key
in the organisation settings yourself, because GitHub keeps their access until
you do. The `private-key` entry is kept for the OpenMetadata role sync (ADR 0014).

## 1. Create the OAuth App (manual fallback)

In the organisation: Settings, Developer settings, OAuth Apps, New OAuth App.
Use an organisation-owned app, not a personal one.

| Field | Value |
| --- | --- |
| Homepage URL | `https://<base_domain>` |
| Authorization callback URL | `https://auth.<base_domain>/callback` |

`<base_domain>` is `spec.access.base_domain` (`olf.localhost` by default).
With `spec.access.issuer: none` (plain HTTP on `*.localhost`, development
only) use `http://<base_domain>` and `http://auth.<base_domain>/callback`.
Generate a client secret. An organisation that restricts third-party access
must approve the app.

## 2. Profile

```yaml
spec:
  identity:
    issuer: github
    github:
      organization: OpenLakeForge
      teams:
        platform-admin: [olf-platform-admins]
        data-engineer: [olf-data-engineers]
        analyst: [olf-analysts]
        viewer: [olf-viewers]
      oauth_app_secret_ref:
        name: github-oauth-app
```

Team values are slugs as they appear in the team URL. Only members of these
teams can sign in; organisation ownership grants nothing.

## 3. Create the Secret (manual fallback)

The Secret lives in the shared namespace (`olf-system` by default). The first
`olf deploy` creates that namespace and then stops with the exact command if
the Secret is missing:

```bash
kubectl -n olf-system create secret generic github-oauth-app \
  --from-literal=client-id="$GITHUB_CLIENT_ID" \
  --from-literal=client-secret="$GITHUB_CLIENT_SECRET"
```

Re-run `olf deploy`. The key names are `client_id_key` and `client_secret_key`
under `oauth_app_secret_ref` (`client-id` and `client-secret` by default).

## CI and the nightly

No real GitHub is involved. `openlakeforge.conformance.yaml` selects
`issuer: github` with `github.host: olf-e2e-github.olf-system.svc`, the host of
the GitHub stub `olf e2e run` starts in the cluster (`olf/e2e/_identity_github.py`).
Dex still reads the Secret named by `oauth_app_secret_ref`, so the nightly
(`.github/workflows/local-full-e2e.yml`) runs `olf platform apply`, which
creates the foundation and the `olf-system` namespace and stops at the missing
Secret, creates `github-oauth-app` with the dummy values `olf-e2e` / `olf-e2e`,
and applies again. The stub accepts any client id and secret.
