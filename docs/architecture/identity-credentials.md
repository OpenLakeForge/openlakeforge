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
out of state. The Keycloak module therefore has no `random_password` and no
Terraform-managed Secret: an idempotent in-cluster bootstrap Job
(`keycloak-credentials-<revision>`, `modules/identity/keycloak/credentials.tf`)
creates each identity Secret **only if it is missing**. Terraform handles
references (`secret_ref{name,key}`, `secretKeyRef`) and never creates, reads or
hashes the value. `olf check contracts` fails if an identity module declares a
`random_*` resource, a Secret resource or a Secret data source, or has a
sensitive or unwrapped output.

Clusters deployed before this change hold Secrets Terraform generated.
`removed { lifecycle { destroy = false } }` blocks make state forget them
without deleting them, and the Job keeps an existing Secret, so the upgrade
rotates nothing. Their old values remain in earlier state files until those
are discarded.

| Rule | Behaviour |
| --- | --- |
| Reuse | An existing Secret with the expected key is kept. Redeploy is not rotation. |
| Commit point | `kubectl create secret` is the only write, one API call per Secret, and fails if the Secret exists, so a Job never overwrites a value a consumer may already hold and no partial Secret exists. |
| Direction | The Secret is the source of truth; the realm Job pushes it into Keycloak (it reads client secrets through `secretKeyRef`). Nothing is read back out of Keycloak. |
| Change detection | Replacing the bootstrap Job replaces the realm Job (`replace_triggered_by`), so the realm re-applies without Terraform hashing a Secret value. |
| Rotation | Explicit: delete the Secret, replace the bootstrap Job, restart consumers ([runbook](../setup/identity-secret-rotation.md)). |
| Job RBAC | `create` on Secrets in its own namespace (cannot be limited by `resourceNames`; #46 inventories this exception); `get` only on the five Secrets it owns. |

### Missing-secret recovery

| Situation | Behaviour |
| --- | --- |
| A client Secret is deleted | When the bootstrap Job is next replaced it regenerates it and the realm Job overwrites the client's secret in Keycloak. This is a rotation of that client: consumers restart, and the Job logs that it regenerated. |
| `keycloak-admin-creds` is deleted, Keycloak's database still has the admin | A new value would not match the stored admin, and Keycloak ignores the Secret once the admin exists. The bootstrap does not regenerate it silently (it refuses while the Keycloak Deployment exists); it fails with a pointer to the admin recovery in [identity-sessions-and-recovery.md](identity-sessions-and-recovery.md). |
| A Secret exists but lacks the expected key | Fail with the Secret and key named; never patch a foreign Secret. |
| Operator-provided Secret (SMTP, upstream SSO, `issuer: external` clients) is missing | Fail before deploying a consumer, naming the Secret and key the profile references. The bootstrap never generates these. |

### Interrupted bootstrap

| Interrupted at | Result on re-run |
| --- | --- |
| Before a value is stored | Nothing persisted, no consumer saw it; regenerated. |
| After some Secrets are created | Created ones are reused, the rest created. |
| After the Secrets, before the realm Job applied them | Keycloak still holds the old values; the realm Job re-run applies the Secrets. Consumers that started early fail token requests until it does, then recover on retry. |
| During a rotation (Secret deleted, new one created, consumers not restarted) | Same as above; the verification in the rotation runbook (refused old value, accepted new one) is the completion check. |

Precedent already in the tree: the Polaris bootstrap Job creates the per-stage
principal Secrets and handles "principal exists, Secret missing" by rotating
the principal; the OpenMetadata bootstrap Job writes the ingestion-bot Secret.
Both create the Secret in-cluster and Terraform never reads it.

## Identity credentials

| Credential | Purpose | Generator | Secret reference | Consumer | Rotation / recovery (owner: platform operator) | Terraform observes value |
| --- | --- | --- | --- | --- | --- | --- |
| Keycloak admin (exists) | First start on an empty database; realm Job; e2e admin API | Bootstrap Job, 32 random alphanumeric characters | `keycloak-admin-creds`, keys `username`, `password` | Keycloak, `keycloak-config-cli` Job, `olf e2e` | [`identity-secret-rotation.md`](../setup/identity-secret-rotation.md#the-bootstrap-admin); lost Secret: see recovery above | No |
| OIDC client `perimeter`, `superset`, `openmetadata`, `trino` (exists) | Client authentication to the token endpoint | Bootstrap Job, 32 random alphanumeric characters | `keycloak-client-<consumer>`, key `client-secret`; with `issuer: external`, operator-provided `oidc-client-<consumer>` | Realm Job today; oauth2-proxy (#176), Superset and OpenMetadata (#25), Trino UI (#26) when wired | [`identity-secret-rotation.md`](../setup/identity-secret-rotation.md) | No |
| Perimeter cookie secret (planned) | Signs and encrypts oauth2-proxy session cookies | Bootstrap Job, 32 random bytes | Name fixed by #176 | oauth2-proxy | Delete, re-run bootstrap, restart the proxy; signs out every user once | No |
| SMTP credentials (planned, optional) | Invitation and password-recovery email | Operator | Profile carries a reference only; value in an operator-created Secret | Keycloak realm (email settings) | Operator edits the Secret, re-runs the realm Job | No |
| Upstream SSO client credentials (planned, optional) | Keycloak brokering to a company IdP | Operator, issued by the upstream IdP | Same: reference in the profile, value in an operator-created Secret | Keycloak identity-provider config | Rotated at the upstream IdP, then the Secret and realm Job | No |
| `olf-users` admin service-account client (planned, #331) | `olf users` writes users and role assignments | Bootstrap Job; client limited to user and group management in the `openlakeforge` realm | Name fixed by #331 | `olf users`, which reads it through kubectl RBAC | Delete, re-run bootstrap; the realm Job updates the client | No |
| Trino workload credential (planned, #26) | Authenticate Superset, dbt and OpenMetadata to Trino | Undecided in #26. Today Trino has no authentication | None | Superset, dbt, OpenMetadata | Not defined until #26 chooses the mechanism | Not decided |

No row serves two classes (#178): the OIDC clients and the perimeter secret are
class 1 plumbing, the Keycloak admin and `olf-users` client are operator
tooling, and the workload credentials below are class 2 and are never members
of a canonical role.

## Existing workload and datastore credentials (residual debt)

Scope rule from #181: credentials not touched by local identity work are listed
here, not migrated. All are in the debt register
([`technical-debt.md`](../technical-debt.md)).

| Credential | Generator | Secret reference | Terraform observes value |
| --- | --- | --- | --- |
| PostgreSQL admin, per-service database users (including `keycloak`) | `random_password` | `postgresql-*-creds` Secrets | Yes |
| SeaweedFS S3 admin and per-stage keys | `random_id` and `random_password` | Secrets from the SeaweedFS module | Yes |
| Polaris root client | `random_password` | `var.bootstrap_secret_name` | Yes |
| Superset `SECRET_KEY` | `random_password` | Helm `extraSecretEnv` | Yes |
| Superset admin and OpenMetadata admin | Literal default `admin` / `admin` (module variable defaults; the local root does not override them) | Helm values; `olf` reads `SUPERSET_ADMIN_PASSWORD` and `OPENMETADATA_ADMIN_PASSWORD` with the same defaults | Yes (a constant) |
| Polaris per-stage principals (Trino, Floe, OpenMetadata, deployer) | Polaris, written by its bootstrap Job | `trino_credentials_secret_name` and siblings in the catalog contract | No |
| OpenMetadata ingestion-bot JWT | OpenMetadata, written by its bootstrap Job | `openmetadata-ingestion-bot`, replicated to governed stage namespaces | No |

The OpenMetadata bootstrap Job deletes and recreates the ingestion-bot Secret
each time it runs, so a re-run of that Job is a rotation. Whether a redeploy
re-runs it was not verified here.

## Not claimed

The bootstrap Job's unit-level guard is the `olf check contracts` rule above.
Verified on the local kind stack when the Job landed: an upgrade from
Terraform-generated Secrets and a later redeploy left all five Secrets
unchanged (uid, resourceVersion and data hash), no Secret value is in Terraform
state or the realm ConfigMap, and the rotation drill, the missing-key and the
missing-admin failures behave as the tables above say. Not exercised: the
operator-provided Secret rows (SMTP, upstream SSO), which nothing consumes yet.
