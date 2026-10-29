# Identity credential inventory

The inventory #181 requires before any identity implementation produces a new
credential. It records what exists on the local stack today, what v0.5-alpha
adds, and the rule for where new credentials come from. Kubernetes Secrets
stay the storage mechanism; Vault, External Secrets Operator and managed
backends are deferred. Scope is local: no AWS account or remote state is
needed, and cloud rows stay contract-shaped only (ADR 0014).

Status words: **exists** means the row is implemented in the tree; **planned**
is a decision for a later change, not code.

## Generate outside Terraform

Marking a Terraform value sensitive hides it from output; it does not keep it
out of state. The Dex module therefore has no `random_password` and no
Terraform-managed Secret: an idempotent in-cluster bootstrap Job
(`dex-credentials-<revision>`, `modules/identity/dex/credentials.tf`)
creates each identity Secret **only if it is missing**. Terraform handles
references (`secret_ref{name,key}`, `secretKeyRef`) and never creates, reads or
hashes the value. `olf check contracts` fails if an identity module declares a
`random_*` resource, a Secret resource or a Secret data source, or has a
sensitive or unwrapped output.

The in-cluster PostgreSQL module follows the same rule (#400): the Job
`postgresql-credentials-<hash>` creates the admin, Polaris and per-database
Secrets if missing, and copies each database password into the namespaces that
run its consumers. Rotating a database password is not automated: PostgreSQL
applies a new value only when its bootstrap Job re-runs. The admin password is
set once, when the data volume is first initialised, so deleting
`postgresql-admin-creds` on a live volume locks the platform out of PostgreSQL.
Back that Secret up with the data volume. `olf check contracts` applies the
no-`random_*`, no-Secret rule to this module too.

Clusters deployed before this change hold Secrets Terraform generated.
`removed { lifecycle { destroy = false } }` blocks make state forget them
without deleting them, and the Job keeps an existing Secret, so the upgrade
rotates nothing. Their old values remain in earlier state files until those
are discarded.

| Rule | Behaviour |
| --- | --- |
| Reuse | An existing Secret with the expected key is kept. Redeploy is not rotation. |
| Commit point | `kubectl create secret` is the only write, one API call per Secret, and fails if the Secret exists, so a Job never overwrites a value a consumer may already hold and no partial Secret exists. |
| Direction | The Secret is the source of truth; Dex reads the client secrets through `secretEnv` at start. Nothing is read back out of Dex. |
| Rotation | Explicit: `olf secrets rotate dex-client-perimeter` (or `-trino`) deletes the Secret, replaces the bootstrap Job, and restarts Dex and the consumers; `superset` and `openmetadata` stay manual ([runbook](../../guides/identity/rotate-secrets.md)). |
| Job RBAC | `create` on Secrets in its own namespace (cannot be limited by `resourceNames`; #46 inventories this exception as E1 in [ADR 0017](../../adr/0017-local-network-policy-and-rbac.md)); `get` only on the four consumer client Secrets it owns. The Trino Job has the same shape: `create` per namespace, `get` and `update` only on its own Secrets (`trino-password-file`, and `trino-credential` in a stage namespace). |

### Missing-secret recovery

| Situation | Behaviour |
| --- | --- |
| A client Secret is deleted | When the bootstrap Job is next replaced it regenerates it. This is a rotation of that client: Dex and the consumers restart, and the Job logs that it created the Secret. |
| A Secret exists but lacks the expected key | Fail with the Secret and key named; never patch a foreign Secret. |
| Operator-provided Secret (the GitHub OAuth App, `issuer: external` clients) is missing | Fail before deploying a consumer, naming the Secret and key the profile references. The bootstrap never generates these. The GitHub OAuth App Secret is enforced today by `olf deploy` before the platform apply (an interactive run creates the GitHub App inline instead, #428); the external-client Secrets are not yet. |

### Interrupted bootstrap

| Interrupted at | Result on re-run |
| --- | --- |
| Before a value is stored | Nothing persisted, no consumer saw it; regenerated. |
| After some Secrets are created | Created ones are reused, the rest created. |
| After the Secrets, before Dex restarted | Dex still holds the old values. Consumers that started on the new ones fail token requests until Dex restarts, then recover on retry. |
| During a rotation (Secret deleted, new one created, consumers not restarted) | Same as above; a completed sign-in, as the rotation runbook says, is the completion check. |

Precedent already in the tree: the Polaris bootstrap Job creates the per-stage
principal Secrets and handles "principal exists, Secret missing" by rotating
the principal; the OpenMetadata bootstrap Job writes the ingestion-bot Secret.
Both create the Secret in-cluster and Terraform never reads it.

## Identity credentials

| Credential | Purpose | Generator | Secret reference | Consumer | Rotation / recovery (owner: platform operator) | Terraform observes value |
| --- | --- | --- | --- | --- | --- | --- |
| OIDC client `perimeter`, `superset`, `openmetadata`, `trino` (exists) | Client authentication to the token endpoint | Bootstrap Job, 32 random alphanumeric characters | `dex-client-<consumer>`, key `client-secret`; with `issuer: external`, operator-provided `oidc-client-<consumer>` | Dex; oauth2-proxy, Superset, OpenMetadata and Trino | [Rotate secrets](../../guides/identity/rotate-secrets.md) | No |
| Perimeter cookie secret (exists) | Signs and encrypts oauth2-proxy session cookies | In-cluster Job `oauth2-proxy-cookie-<hash>`, 32 random characters, create-if-missing | `oauth2-proxy-cookie`, key `cookie-secret` | oauth2-proxy | Delete the Secret, replace the Job, restart the proxies; signs out every user once | No |
| Trino workload credentials (#26) | Authenticate Superset, dbt and OpenMetadata ingestion to Trino, one principal each: `olf-<stage>-runtime` per stage and `openmetadata` | In-cluster Job `trino-credentials-<hash>` (`modules/query/trino/credentials.tf`), create-if-missing, 32 random alphanumeric characters; local root only | `trino-credential-<principal>` in `olf-system`, keys `username`, `password`; each stage's own principal replicated as `trino-credential` into that stage namespace only ([ADR 0016](../../adr/0016-trino-workload-authentication.md)) | Superset, dbt run pods, OpenMetadata ingestion, `olf e2e` and `olf query`, each from its Secret by reference; Trino authenticates them when `trino_workload_authentication` is `password`, the local default with `spec.access` | Delete the Secret, replace the Job, re-sync consumers (ADR 0016) | No |
| GitHub OAuth App (exists) | Dex's GitHub connector authenticates to GitHub | Operator, issued by GitHub | `spec.identity.github.oauth_app_secret_ref`: name, `client_id_key` (default `client-id`), `client_secret_key` (default `client-secret`); a Secret in `olf-system` the operator creates ([how](../../guides/identity/connect-github.md)) | Dex, through `secretKeyRef` | Rotated in GitHub, then the Secret, then a Dex restart | No |
| Trino password file and internal secret (exists, not yet used, #26) | `password.db` is the PBKDF2 derivation of the principal Secrets, rebuilt on every Job run and never a source of truth; the internal secret is Trino's `internal-communication.shared-secret` | Same Job; the file is derived, the internal secret is create-if-missing | `trino-password-file` (key `password.db`) and `trino-internal-secret` (key `internal-communication.shared-secret`) in `olf-system` | Trino, once authentication is on | The file follows its principal Secrets; the internal secret is rotated like a principal Secret | No |

No row serves two classes (#178): the OIDC clients and the perimeter secret are
class 1 plumbing, and the workload credentials below are class 2 and are never members
of a canonical role.

## Existing workload and datastore credentials (residual debt)

Scope rule from #181: credentials not touched by local identity work are listed
here, not migrated. All are in the debt register
([`technical-debt.md`](../../technical-debt.md)).

| Credential | Generator | Secret reference | Terraform observes value |
| --- | --- | --- | --- |
| PostgreSQL admin, per-service database users (including `dex`) and the Polaris database user (#400) | In-cluster Job `postgresql-credentials-<hash>` (`modules/storage/postgresql/credentials.tf`), create-if-missing, 32 random alphanumeric characters | `postgresql-*-creds` Secrets, one copy per consuming namespace | No |
| SeaweedFS S3 admin and per-stage keys | `random_id` and `random_password` | Secrets from the SeaweedFS module | Yes |
| Polaris root client (#400) | Polaris module Job `polaris-token-key-<hash>`, in-cluster, create-if-absent, 32 random alphanumeric characters | `polaris-bootstrap-credentials` | No |
| Superset `SECRET_KEY` (#400) | In-cluster Job `superset-credentials-<hash>` in each stage namespace (`modules/analytics/superset/credentials.tf`), create-if-missing; reuses the key in the chart Secret `superset-env` when an earlier release made it | `superset-credentials` (`secret-key`), read by the pods through `secretKeyRef` | No |
| Superset admin and OpenMetadata admin | Literal default `admin` / `admin` (module variable defaults; the local root does not override them) | Helm values; `olf` reads `SUPERSET_ADMIN_PASSWORD` and `OPENMETADATA_ADMIN_PASSWORD` with the same defaults | Yes (a constant) |
| Polaris token-signing key | Polaris module Job, in-cluster, create-if-absent | `<release>-token-broker` (`symmetric.key`) | No |
| OpenMetadata Fernet key (encrypts stored credentials, ADR 0016) | OpenMetadata module Job, in-cluster, create-if-absent | `<release>-fernet-key` (`fernet-key`) | No |
| Polaris per-stage principals (Trino, Floe, OpenMetadata, deployer) | Polaris, written by its bootstrap Job | `trino_credentials_secret_name` and siblings in the catalog contract | No |
| OpenMetadata ingestion-bot JWT | OpenMetadata, written by its bootstrap Job | `openmetadata-ingestion-bot`, replicated to governed stage namespaces | No |

The OpenMetadata bootstrap Job deletes and recreates the ingestion-bot Secret
each time it runs, so a re-run of that Job is a rotation. Whether a redeploy
re-runs it was not verified here.

## Exposure scan

`olf e2e run --env local` runs `identity credential exposure`
(`tools/olf/olf/e2e/_secret_exposure.py`) after every other assertion except the
workload secret rotation drill, which comes last so the scan checks the
credentials that were live for the whole run. It reads the **current** values of
the identity Secrets from the cluster and looks for those exact strings: the
four OIDC client secrets (names from the identity contract), the perimeter
cookie secret (`oauth2-proxy-cookie`) and the Trino workload credentials. It
also looks for each value base64-encoded (the form Kubernetes stores Secret data
in) and JSON-escaped (the form in `-o json` and Terraform state). A failure names
the surface and the Secret (`name/key`), never the value.

With an external issuer (`identity.external_oidc`) the operator-provided client
Secrets are scanned when present. When an earlier
assertion fails, the scan still runs (best effort) and only logs its result, so
the run reports the original failure.

| Surface | What is read |
| --- | --- |
| Terraform state | The platform root's and the foundation's state files (source checkout: `terraform.tfstate` in each root; installed: under `OPENLAKEFORGE_TERRAFORM_STATE_ROOT`). |
| ConfigMaps | `kubectl get configmap -o json` in the platform namespace and every stage namespace in the provider contract, not only the stage the run selected. |
| Diagnostics | The bundle `olf diagnostics collect` writes for each of those namespaces. |
| Logs | `kubectl logs --all-containers --tail=2000` of every pod in those namespaces, which includes completed Job pods. |

A surface that cannot be read (no state file yet, a failed `kubectl`) is
logged as skipped, not passed. A value shorter than 16 characters is not
scanned for, because it would match ordinary text; the generated secrets are 32
characters, so this only affects a short operator-chosen secret, which is then
logged as not scanned.

Not covered:

- Helm release Secrets (`sh.helm.release.v1.*`) hold the rendered values
  compressed, and are not decoded. Rendered values reach the scan only through
  ConfigMaps and logs.
- CI logs and uploaded CI artifacts: the scan sees the cluster's logs, not the
  workflow's own output.
- Pod logs older than the tail, logs of pods that were replaced before the
  scan, logs of a container's previous instance after a restart (`kubectl logs
  --previous` is not collected), and any surface outside the platform and stage
  namespaces.
- A failure before the assertions start (the pod readiness check) does not run
  the scan.
- Operator-chosen secrets (external-issuer client secrets) containing `<`, `>` or `&` are not matched in Kubernetes JSON output, which Go encodes as `\u003c`, `\u003e`, `\u0026`; generated secrets are alphanumeric and unaffected.
- Old Terraform state files left from before the bootstrap Job (see "Generate
  outside Terraform"): only the current state is read.
- Only the current values are searched, so a rotated-out value that leaked
  earlier is not found.
- The local e2e exercises the scan's wiring (`kubectl`, `olf diagnostics
  collect`, state paths) on a cluster, and it passes there; the rotation drill
  runs after it, so the credential the drill replaces is not scanned afterwards.

## Not claimed

The bootstrap Job's unit-level guard is the `olf check contracts` rule above.
The drills for the earlier issuer module (upgrade from Terraform-generated
Secrets, the missing-key and missing-admin failures) are not carried over; the
same Job shape now lives in `modules/identity/dex`. Rotation there is
exercised by the local e2e's workload secret rotation drill (`dex-client-perimeter`, exercised in the local
e2e on 2026-10-23, took about 100 s); the `superset` and `openmetadata`
clients and the Polaris token key are not.
