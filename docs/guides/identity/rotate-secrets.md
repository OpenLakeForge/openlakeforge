# Rotate secrets

How to rotate the secrets sign-in depends on with `olf secrets rotate`, and the
manual procedure for the clients the command does not cover.

Rotate after a suspected leak, when someone who could read the Secrets leaves,
or on a schedule. The command is for `provider: local` with
`identity.issuer: github`. With `issuer: external`, rotate the client secrets
in your issuer and in the `oidc-client-<consumer>` Secrets, then restart the
consumers ([Bring your own OIDC issuer](external-issuer.md)).

## `olf secrets rotate`

```bash
olf secrets rotate dex-client-perimeter
```

Add `-f <profile>` if you deployed from a profile file. The command prints what
it rotated and restarted, and never prints a secret value.

| Target | Rotates | Restarts |
| --- | --- | --- |
| `dex-client-perimeter` | The perimeter's Dex client secret | `deployment/dex` and the perimeter deployments |
| `dex-client-trino` | Trino's Dex client secret | `deployment/dex` and `deployment/trino-coordinator` |
| `github-app` | Registers a new GitHub App through `olf github connect` and replaces its Secret | `deployment/dex` |

For a Dex client, the command deletes the Secret, applies only the credentials
Job (a targeted Terraform apply with `-replace`) so the Secret is made again,
restarts what reads it, waits for the rollouts, and ends with
`Confirm: sign in at <portal URL>`. Signing in succeeds only when Dex and the
service hold the same secret.

### What it refuses

Before it deletes anything, it stops and names the cause when:

- the provider is not `local`, or `identity.issuer` is not `github`;
- the platform state has drifted: run `olf deploy` first;
- the Secret lacks the `client-secret` key;
- another `dex-client-*` Secret is missing, because the shared Job would
  regenerate that one too, without restarting its consumers: run `olf deploy`
  first.

### The GitHub App

`olf secrets rotate github-app` registers a new GitHub App in your browser,
replaces the Secret named by `oauth_app_secret_ref` and restarts Dex. Then:

1. Install the new app on the organization (the command prints the link).
2. Delete the old GitHub App and its private key in the organization settings.

`--no-browser` prints the URL instead of opening it.

To rotate only the client secret by hand, generate a new one in GitHub, update
the Secret, and restart Dex:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
KUBECTL="$(olf toolchain path kubectl)"
"$KUBECTL" -n olf-system rollout restart deployment dex
```

Rotating `dex-client-perimeter` is exercised in the local end-to-end run; it
took about 100 seconds there. That is one measurement, not a bound.

## Manual: the Superset and OpenMetadata clients

The command does not cover `superset` and `openmetadata`: Superset keeps a copy
of its secret per stage, and OpenMetadata has no consumer step documented.
For `superset`, with the managed kubectl and kubeconfig set as above:

1. Delete the Secret:

   ```bash
   "$KUBECTL" -n olf-system delete secret dex-client-superset
   ```

2. Re-run the platform phase with the bootstrap Job replaced, so it recreates
   the missing Secret. Each stage keeps its own Superset copy, made by the
   one-shot `kubernetes_job_v1.oidc_secret` of that stage's `module.superset`:
   add a `-replace` of that Job for each stage.

   ```bash
   TF_CLI_ARGS_apply='-replace=module.dex[0].kubernetes_job_v1.credentials -replace=module.superset["<stage>"].kubernetes_job_v1.oidc_secret[0]' \
     olf deploy --provider local --phase platform
   ```

3. Restart Dex, then Superset in each stage:

   ```bash
   "$KUBECTL" -n olf-system rollout restart deployment dex
   ```

4. Sign in through the portal.

If the bootstrap Job fails with `Secret <name> exists without key
client-secret`, something else created that Secret: fix or delete it and re-run
step 2. An interrupted bootstrap is safe to re-run.

## The session cookie secret

Rotating it signs everyone out:

```bash
"$KUBECTL" -n olf-system delete secret oauth2-proxy-cookie --ignore-not-found
TF_CLI_ARGS_apply='-replace=module.perimeter.kubernetes_job_v1.cookie_secret' \
  olf deploy --provider local --phase platform
"$KUBECTL" -n olf-system rollout restart deployment -l openlakeforge.io/role-floor
```

Polaris token-key rotation has no documented procedure and is not covered.
