# Caller and credential inventory

Every authentication path on the local stack, classified into exactly one class
(#178). Written from the tree before the perimeter (#176), the integrations
(#25, #26) and the network policy (#46) change anything; it is verified against
the final runtime when those land. Rows state what the code does today. Where a
fact was not checked the cell says **to verify**.

## Classes

| Class | Who | Governed by | Never |
| --- | --- | --- | --- |
| 1. Human | A person reaching a service UI through the issuer | Canonical roles, enforced at the perimeter (ADR 0014) | Used by an automated path |
| 2. Workload / automation, no cloud API | A pod calling another component, or `olf` and CI calling the cluster | A service credential from a Secret, or Kubernetes RBAC | A human account; routed through the perimeter; a member of a canonical role |
| 3. Cloud workload | A pod calling a cloud API (S3, Glue, Secrets Manager) | Cloud IAM through Pod Identity / workload identity (#45) | A canonical role |

Classification rule: a caller that reaches a service **through the issuer** is
class 1. A caller that authenticates with a service credential or Kubernetes
RBAC is class 2, whether it runs in a pod or on a workstation, and even when a
person started it. `olf` and CI are therefore class 2 with Kubernetes RBAC as
their identity. (Interpretation of #178 for out-of-cluster callers; confirm.)

Class 3 is inventoried for clarity only. Exercising cloud credentials or their
rotation is outside this milestone.

## Paths

Columns: caller location; endpoint and transport; credential or RBAC boundary;
principal and stage scope; Secret reference; rotation.

### Class 1

| Path | Caller | Endpoint / transport | Credential | Scope | Secret | Rotation |
| --- | --- | --- | --- | --- | --- | --- |
| Browser through the perimeter (#176) | Person's browser | `https://<service>.<base_domain>` via Traefik, forwardAuth to oauth2-proxy | Issuer login, perimeter cookie | Canonical role against `shared.identity.roles`, every enabled stage | None (cookie secret in [identity-credentials.md](identity-credentials.md)) | Session policy in [identity-sessions-and-recovery.md](identity-sessions-and-recovery.md) |
| Browser to Superset / OpenMetadata native OIDC (#25, not built) | Person's browser | Same route; the app redirects to the issuer | Issuer login, native session | Role claim mapped by the role model | Client Secrets `keycloak-client-superset` / `-openmetadata` | Client rotation runbook |

### Class 2: `olf`, CI and workstation

| Path | Caller | Endpoint / transport | Credential / boundary | Scope | Secret | Rotation |
| --- | --- | --- | --- | --- | --- | --- |
| Platform and bootstrap (`olf deploy`, `destroy`, `check`, `diagnostics`) | Workstation or CI runner | Kubernetes API through kubeconfig; Terraform, Helm, kubectl | Kubernetes RBAC; the local kind kubeconfig is cluster-admin | Whole cluster | None | Kubeconfig lifecycle belongs to the cluster |
| Artifact deployment (`olf artifacts upload-manifests`, `deploy-optional-layers`) | Workstation or CI | `kubectl port-forward` to the SeaweedFS Service (`s3.port_forward_client`); credentials read by `k8s.secret_value` | RBAC (`secrets` get, `pods/portforward`) plus an S3 key | The ops-bucket Secret named by the contract, default `seaweedfs-s3-creds`: the SeaweedFS S3 admin key, not a stage key (**to verify** against the contract) | `seaweedfs-s3-creds`, keys `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Terraform-generated; residual debt |
| Catalog reconciliation (`olf catalog sync-namespaces`) | Workstation or CI | `kubectl port-forward` to Polaris | OAuth client credentials of the Polaris `deployer` principal | Deployer principal's catalog roles | `polaris-deployer-creds` (name from the contract) | Polaris bootstrap Job |
| Metadata reconciliation (`olf openmetadata deploy-metadata`) | Workstation or CI | `access.service_url`: the ingress route when the contract has routes (the local default), a port-forward otherwise, with `OLF_PORT_FORWARD=1`, or for a route behind an enabled perimeter | OpenMetadata native admin login, email and password from `OPENMETADATA_ADMIN_EMAIL` / `_PASSWORD`, defaults `admin@open-metadata.org` / `admin` | Instance-wide admin | None; env default | None |
| Superset report deployment (`olf report import`) | Workstation or CI | Same `access.service_url` | Superset native admin login, `SUPERSET_ADMIN_USERNAME` / `_PASSWORD`, defaults `admin` / `admin` | Instance-wide admin | None; env default | None |
| Workstation e2e (`olf e2e run`) | Workstation | kubectl exec and run, and port-forwards (Trino CLI pod, Dagster, portal) | RBAC; the Trino CLI runs as `--user olf-<stage>-runtime` with no password today, over HTTPS to `localhost:8443` when `trino_workload_authentication` is `tls` or `password`, and with the password after the #26 cutover ([ADR 0016](../adr/0016-trino-workload-authentication.md), step 4 pending). The identity and perimeter checks also use the Keycloak master `admin` (`keycloak-admin-creds`, read through RBAC) and the ingress hostnames; the perimeter check signs in over HTTP as throwaway per-role users | Creates and deletes per-role test users | `keycloak-admin-creds` | Admin rotation runbook |
| CI (`.github/workflows/local-full-e2e.yml`) | GitHub runner | The same `olf` commands against a kind cluster created on the runner | The ephemeral cluster's kubeconfig. The only GitHub secret the workflow uses is `GITHUB_TOKEN`, for the registry | Ephemeral cluster | None for platform credentials | Cluster is discarded |

### Class 2: in-cluster

| Path | Caller | Endpoint / transport | Credential / boundary | Scope | Secret | Rotation |
| --- | --- | --- | --- | --- | --- | --- |
| Superset to Trino | Superset pod, stage namespace | `trino:8080` ClusterIP, plain HTTP; HTTPS on `tls_port` with the CA mounted from `olf-local-ca` when `trino_workload_authentication` is not `off` | **None.** The SQLAlchemy URI carries a user name (`trino://<user>@...`). `olf` rewrites it to the stage's `olf-<stage>-runtime` from the contract. Decided, migration pending (#26): password file over HTTPS, one verified principal per stage ([ADR 0016](../adr/0016-trino-workload-authentication.md)) | Trino catalog rules match that string | None today; after migration the stage's `trino-credential` | None today; per [ADR 0016](../adr/0016-trino-workload-authentication.md) |
| dbt to Trino (Dagster run pod) | Run pod | Same Trino Service | **None**; `OPENLAKEFORGE_DBT_TRINO_USER` is `olf-<stage>-runtime`. Over HTTPS with the CA mounted from `olf-local-ca` when `trino_workload_authentication` is not `off`. Decided, cutover pending (#26): dbt-trino `method: ldap` ([ADR 0016](../adr/0016-trino-workload-authentication.md)) | Same | None today; after migration the stage's `trino-credential` | None today; per [ADR 0016](../adr/0016-trino-workload-authentication.md) |
| Trino to Polaris | Trino pod | Polaris REST in the shared namespace | OAuth client credentials of the stage's Trino principal | One stage catalog | `trino_credentials_secret_name`, keys `POLARIS_TRINO_CLIENT_ID` / `_SECRET` | Rotate the Polaris principal; the bootstrap Job rotates a principal whose Secret is gone |
| Floe runner to Polaris | Floe Job | Polaris REST | OAuth client credentials of the stage's Floe principal | One stage catalog | `floe_credentials_secret_name`, keys `POLARIS_FLOE_CLIENT_ID` / `_SECRET`, replicated into the stage namespace | Same |
| Floe and Trino to SeaweedFS | Pods | S3 API on the in-cluster Service | Per-stage S3 key | One stage's buckets | Stage S3 Secrets | Terraform-generated |
| OpenMetadata to Trino | OpenMetadata pods; the OLF-owned `openmetadata-trino-ingestion` Job and its hourly CronJob | Trino as user `openmetadata` | A string match (read-only rules), no authentication today. The Job verifies the HTTPS listener at `tls`; OpenMetadata 1.13.6 pipeline-client pods could not receive the CA, which is why OLF owns it. Decided, migration pending (#26): Trino `BasicAuth` over HTTPS as principal `openmetadata` ([ADR 0016](../adr/0016-trino-workload-authentication.md)) | All stage catalogs, read | None today; `trino-credential-openmetadata` after migration | None today; per [ADR 0016](../adr/0016-trino-workload-authentication.md) |
| Polaris `om` principal | Nothing | n/a | A principal the Polaris bootstrap still creates with `CATALOG_MANAGE_CONTENT` on every stage catalog. OpenMetadata crawls through Trino, and no module or `olf` command reads its Secret ([ADR 0015](../adr/0015-polaris-identity-provider-evaluation.md)) | All stage catalogs, write | `om_credentials_secret_name` | Dormant; removal is follow-up scope |
| OpenMetadata ingestion bot | Floe and Dagster pods, governed stages | OpenMetadata API (OpenLineage) | Bot JWT, sent as `OPENLINEAGE_API_KEY` / Floe `secret_name` | Governed stages only | `openmetadata-ingestion-bot`, key from the contract | The OpenMetadata bootstrap Job regenerates it on each run. **To verify** that the bot token keeps validating once OIDC is enabled (#25) |
| Dagster | Pods, `olf e2e`, and anyone who can reach the Service | Dagster webserver, ClusterIP port 80, and its GraphQL API | **No native authentication or per-user authorization.** None is configured in `infra/helm/values/local/dagster.yaml`, and Dagster OSS has none. The ingress route is protected by the perimeter (#176); this in-cluster path is protected only by Kubernetes RBAC (`pods/portforward`, `exec`) until NetworkPolicy (#46, designed in [ADR 0017](../adr/0017-local-network-policy-and-rbac.md)). No NetworkPolicy object exists in the tree today, so any pod can call the Service. See [Dagster boundary](#dagster-boundary) | n/a | None | n/a |
| Dagster run launcher | Dagster pod | Kubernetes API, creating run Jobs | The pod's ServiceAccount | **To verify** (#46) | None | n/a |
| Issuer back-channel | oauth2-proxy, Superset, OpenMetadata pods | `https://auth.<base_domain>` through Traefik, trusting the local CA (`in-cluster-resolution` module) | Client secret at the token endpoint; JWKS is public | One client each | `keycloak-client-<consumer>` | Client rotation runbook. Discovery and `iss` from a pod are checked by `olf e2e` today |
| Datastores | Keycloak, Polaris, Superset, OpenMetadata | PostgreSQL | Per-service database user | Own database | `postgresql-*-creds` | Terraform-generated; residual debt |

### Class 3

| Path | Caller | Boundary | Status |
| --- | --- | --- | --- |
| AWS pods to S3, Glue | Trino, Floe, Dagster | EKS Pod Identity (`identity.aws_pod_identity`) | Contract only; not exercised here |
| Azure pods | Workload pods | `identity.azure_workload_identity_ready` | Contract only; not exercised here |

## Decision: how workstation and CI automation reach services

`olf`, `olf e2e` and CI are not pods and not people. They reach services for
functional checks (deploy a dashboard, reconcile metadata, query Trino, launch
a pipeline) by one of two paths:

1. an **RBAC-authorized `kubectl port-forward`** from the runner to the
   internal ClusterIP Service, bound to loopback; or
2. **in-cluster execution** (`kubectl exec` or a Job) that calls the Service
   by its internal name.

Never through the public hostnames and their browser redirects, and never
authenticated as a human or service-administrator account. Who may
automate is who holds `pods/portforward` and the Secret reads in RBAC; a
claimed user name (`X-Trino-User`, a login) is not proof of identity and
is not the control.

Two cases differ:

- A test of the **perimeter or issuer itself** (redirects, role refusal,
  logout) has to use the public hostnames. It signs in as **per-role test
  users** created for the run through the admin API and deleted afterwards, as
  `olf e2e` already does for the role claim (`e2e/_identity.py`). They hold
  canonical roles and are never used by a deploy or pipeline path.
- The cloud and `issuer: external` cases are unchanged: the runner needs the
  Kubernetes RBAC and the operator-provided Secrets.

**Built.** `access.service_url` port-forwards to every route in
`shared.access.perimeter.protected_routes` when the perimeter is enabled, so
`olf openmetadata deploy-metadata`, `olf report import` and the e2e assertions
reach the internal Service and never meet forwardAuth. Only the ingress drills
in `olf e2e` pass `through_ingress=True`.

**Still open.** How these callers authenticate to Superset and OpenMetadata
once those accept only issuer login (#25): **to verify** whether the native
`db` login of `SupersetClient` and the OpenMetadata basic login survive OIDC
being enabled. Read from source in
[`identity-service-permissions.md`](identity-service-permissions.md): the
Superset REST `db` login should survive; OpenMetadata's basic login does not
(it answers 403 under `custom-oidc`), so those callers move to a bot token.

## Dagster boundary

Dagster OSS has no native authentication and no per-user authorization (#26).
The tree configures none. Three paths reach the same webserver and GraphQL API:

| Path | Who | What stops everyone else |
| --- | --- | --- |
| `https://dagster-<stage>.<base_domain>` through Traefik | A person | The perimeter (ADR 0014, seam 3): only `platform-admin` and `data-engineer` hold the `orchestration` grant in `release/identity-roles.yaml`; every other role and an unauthenticated request is refused before the request reaches Dagster. `olf e2e run --env local` asserts this per route (`e2e/_perimeter.py`) |
| `kubectl port-forward` to the `dagster-dagster-webserver` Service | `olf e2e run` (`e2e/_dagster.py`) and an operator | Kubernetes RBAC: `pods/portforward` in the stage namespace. The forward binds loopback, and the caller needs no session |
| The Service by its in-cluster name | Any pod | Nothing today. NetworkPolicy (#46) is not built |

The two internal paths are class 2 (a workload or operator acting through
Kubernetes RBAC), not an authentication bypass that is reachable from outside
the cluster: the Service is `ClusterIP`, the only route out of the cluster is
the perimeter-protected ingress, and the kind ingress listens on `127.0.0.1`
(ADR 0013). The OpenMetadata Dagster connector is deferred
(`technical-debt.md`), so OpenMetadata does not call Dagster today.
That the internal path is unreachable by an untrusted pod is **not yet
verified**: it needs #46.

Consequence. Whoever is admitted to Dagster, by either path, has the full
Dagster capability: launching jobs with run configuration, terminating and
deleting runs, reloading code locations and toggling schedules (general
Dagster capabilities, not re-checked against the pinned 1.13.7). The pods
that run user code and runs hold the stage's storage and Polaris (Floe)
Secrets, and the OpenMetadata ingestion-bot Secret when governance is enabled
(`runtime_env_secrets` in `modules/orchestration/dagster`). Holding
`data-engineer` therefore means holding all of that, and the role should be
assigned on that basis. Canonical roles do not narrow it: the grant maps both
roles to `full`.

## Known limitations

- **Shared service accounts.** Every class-2 credential is shared by all
  users of the component it fronts: any Superset user queries Trino as the
  stage runtime principal, whatever their role. This is the direct cause of
  analysts sharing data access. Per-user data access is the data-plane
  authorization deferral (#179); it is not solved by anything in v0.5-alpha.
- **Trino principal is a claim.** The catalog rules key on `X-Trino-User`, which
  Trino accepts unauthenticated (register entry "Trino has no client
  authentication" in [`technical-debt.md`](../technical-debt.md)). [ADR 0016](../adr/0016-trino-workload-authentication.md)
  selects password-file authentication over HTTPS and proves a DEV credential
  cannot become PROD; no caller is migrated yet, so the rows above still
  describe the unauthenticated path.
- **Polaris keeps its own client credentials.** Whether the issuer could
  authenticate the catalog callers instead is evaluated in
  [ADR 0015](../adr/0015-polaris-identity-provider-evaluation.md) and deferred.
- **Static `admin` passwords** for Superset and OpenMetadata are the credential
  `olf` metadata and report deployment authenticate with today.
- **Unverified acceptance items** of #178: that class-2 traffic is unreachable
  from outside the cluster, and that ingestion, Superset to Trino and Floe/dbt
  to the catalog run unattended with human SSO on. These need #25, #26, #176
  and #46 and are checked then.
