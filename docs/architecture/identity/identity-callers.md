# Caller and credential inventory

Every authentication path on the local stack, classified into exactly one class
(#178). Written from the tree before the v0.5 identity work and updated for
what it built: Dex with GitHub, the perimeter (#176), Superset and OpenMetadata
sign-in with role sync (#25), the Trino UI sign-in (HTTP: a fixed user behind
the perimeter; HTTPS: OAuth2 through Dex), per-class workload credentials,
default-deny network policy (#46), and `olf infra break-glass` with its drills. Rows
state what the code does. Where a fact was not checked on a running cluster the
cell says **to verify**.

## Classes

| Class | Who | Governed by | Never |
| --- | --- | --- | --- |
| 1. Human | A person reaching a service UI through the issuer | Canonical roles, enforced at the perimeter (ADR 0014) | Used by an automated path |
| 2. Workload / automation, no cloud API | A pod calling another component, or `olf` and CI calling the cluster | A service credential from a Secret, or Kubernetes RBAC | A human account; routed through the perimeter; a member of a canonical role |
| 3. Cloud workload | A pod calling a cloud API (S3, Glue, Secrets Manager) | Cloud IAM through Pod Identity / workload identity (#45) | A canonical role |

Classification rule: the class belongs to the operation. A caller that reaches
a service **through the issuer** is class 1. A caller that authenticates with a
service credential or Kubernetes RBAC is class 2, whether it runs in a pod or
on a workstation, and even when a person started it. `olf infra` commands and CI
are therefore class 2 with Kubernetes RBAC as their identity; `olf` commands
that need no permissions (`init`, `check`, `project validate`) have none. A
Dex-session platform surface is planned and not built
([ADR 0020](../../adr/0020-infrastructure-common-and-platform-cli-surfaces.md)).
The command paths in the rows below are the earlier top-level ones, which still
work as hidden aliases of `olf infra ...`. (Interpretation of #178 for
out-of-cluster callers; confirm.)

Class 3 is inventoried for clarity only. Exercising cloud credentials or their
rotation is outside this milestone.

## Paths

Columns: caller location; endpoint and transport; credential or RBAC boundary;
principal and stage scope; Secret reference; rotation.

### Class 1

| Path | Caller | Endpoint / transport | Credential | Scope | Secret | Rotation |
| --- | --- | --- | --- | --- | --- | --- |
| Browser through the perimeter (#176) | Person's browser | `https://<service>.<base_domain>` via Traefik, forwardAuth to oauth2-proxy | Issuer login, perimeter cookie | Canonical role against `shared.identity.roles`, every enabled stage | None (cookie secret in [identity-credentials.md](identity-credentials.md)) | Session policy in [identity-sessions-and-recovery.md](identity-sessions-and-recovery.md) |
| Browser to Superset / OpenMetadata native OIDC (#25, built) | Person's browser | Same route; the app redirects to the issuer | Issuer login, native session | Role claim mapped by the role model | Client Secrets `dex-client-superset` / `-openmetadata` | Client rotation runbook |

### Class 2: `olf`, CI and workstation

| Path | Caller | Endpoint / transport | Credential / boundary | Scope | Secret | Rotation |
| --- | --- | --- | --- | --- | --- | --- |
| Platform and bootstrap (`olf infra deploy`, `destroy`, `check`, `diagnostics`) | Workstation or CI runner | Kubernetes API through kubeconfig; Terraform, Helm, kubectl | Kubernetes RBAC; the local kind kubeconfig is cluster-admin | Whole cluster | None | Kubeconfig lifecycle belongs to the cluster |
| Revision and activation store (`olf infra project build`, `deploy`, `status`, `revision inspect\|verify`) | Workstation or CI | `kubectl port-forward` to the SeaweedFS Service (`s3.port_forward_client`); credentials read by `k8s.secret_value` | RBAC (`secrets` get, `pods/portforward`) plus an S3 key | The ops-bucket Secret named by the contract, default `seaweedfs-s3-creds`: the SeaweedFS S3 admin key, not a stage key (**to verify** against the contract) | `seaweedfs-s3-creds`, keys `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Terraform-generated; residual debt |
| Catalog reconciliation (`olf infra catalog sync-namespaces`) | Workstation or CI | `kubectl port-forward` to Polaris | OAuth client credentials of the Polaris `deployer` principal | Deployer principal's catalog roles: `CATALOG_MANAGE_METADATA` on its stage catalog, plus `CATALOG_MANAGE_ACCESS` when people sign in, to grant `olf-gold-read` on Gold namespaces (ADR 0019) | `polaris-deployer-creds` (name from the contract) | Polaris bootstrap Job |
| Metadata reconciliation (`olf infra openmetadata deploy-metadata`) | Workstation or CI | `access.service_url`: the ingress route when the contract has routes (the local default), a port-forward otherwise, with `OLF_PORT_FORWARD=1`, or for a route behind an enabled perimeter | The admin bot `olf-automation`, JWT in Secret `openmetadata-automation-bot`; the native admin login (`OPENMETADATA_ADMIN_EMAIL`, default `admin@open-metadata.org`; the password is read from Secret `openmetadata-admin` unless `OPENMETADATA_ADMIN_PASSWORD` is set) is used only while basic login still works, to create the bot, and stops working once sign-in is on | Instance-wide admin | With identity on: `openmetadata-automation-bot`, key `OPENMETADATA_AUTOMATION_JWT`; otherwise Secret `openmetadata-admin`, key `admin-password` | None |
| Superset report deployment (`olf infra report import`) | Workstation or CI | Same `access.service_url` | Superset native admin login; the password is read from Secret `superset-credentials` in the stage namespace unless `SUPERSET_ADMIN_PASSWORD` is set | Instance-wide admin | None; generated per stage | None |
| Workstation e2e (`olf infra e2e run`) | Workstation | kubectl exec and run, and port-forwards (Trino CLI pod, Dagster, portal) | RBAC; the Trino CLI runs as `--user olf-<stage>-runtime` over HTTPS to `localhost:8443` when `trino_workload_authentication` is `tls` or `password`, with the principal's password from `trino-credential-olf-<stage>-runtime` on the CLI's stdin at `password`, the local default with `spec.access`; its isolation probes try a claimed user name with a DEV password, with no password and with a wrong one ([ADR 0016](../../adr/0016-trino-workload-authentication.md)). The perimeter check uses the ingress hostnames and signs in through Dex's GitHub connector against a stub GitHub the e2e runs in `olf-system`, as one stub user per mapped role | Creates and deletes the stub pod, Service and certificate; no real account | None: the stub accepts any code | None |
| CI (`.github/workflows/local-full-e2e.yml`) | GitHub runner | The same `olf` commands against a kind cluster created on the runner | The ephemeral cluster's kubeconfig. The only GitHub secret the workflow uses is `GITHUB_TOKEN`, for the registry | Ephemeral cluster | None for platform credentials | Cluster is discarded |

### Class 2: in-cluster

| Path | Caller | Endpoint / transport | Credential / boundary | Scope | Secret | Rotation |
| --- | --- | --- | --- | --- | --- | --- |
| Superset to Trino | Superset pod, stage namespace | `trino:8080` ClusterIP, plain HTTP; HTTPS on `tls_port` with the CA mounted from `olf-local-ca` when `trino_workload_authentication` is not `off` | At `password` (the local default with `spec.access`): the stage principal's password, set by `olf infra report import` through the import's `passwords` field. Otherwise **none**: the SQLAlchemy URI carries only a user name (`trino://<user>@...`), which `olf` sets to the stage's `olf-<stage>-runtime` from the contract ([ADR 0016](../../adr/0016-trino-workload-authentication.md)) | Trino catalog rules match that principal | The stage's `trino-credential`, read by `olf infra report import` | Rotation re-runs the import; per [ADR 0016](../../adr/0016-trino-workload-authentication.md) |
| dbt to Trino (Dagster run pod) | Run pod | Same Trino Service | `OPENLAKEFORGE_DBT_TRINO_USER` is `olf-<stage>-runtime`. Over HTTPS with the CA mounted from `olf-local-ca` when `trino_workload_authentication` is not `off`; at `password` dbt-trino `method: ldap` with the stage's password ([ADR 0016](../../adr/0016-trino-workload-authentication.md)) | Same | At `password`, the stage's `trino-credential` through a `secretKeyRef` | Run pods read it at launch; per [ADR 0016](../../adr/0016-trino-workload-authentication.md) |
| Trino to Polaris | Trino pod | Polaris REST in the shared namespace | OAuth client credentials of the stage's Trino principal | One stage catalog | `trino_credentials_secret_name`, keys `POLARIS_TRINO_CLIENT_ID` / `_SECRET` | Rotate the Polaris principal; the bootstrap Job rotates a principal whose Secret is gone |
| Floe runner to Polaris | Floe Job | Polaris REST | OAuth client credentials of the stage's Floe principal | One stage catalog | `floe_credentials_secret_name`, keys `POLARIS_FLOE_CLIENT_ID` / `_SECRET`, replicated into the stage namespace | Same |
| Floe and Trino to SeaweedFS | Pods | S3 API on the in-cluster Service | Per-stage S3 key | One stage's buckets | Stage S3 Secrets | Terraform-generated |
| OpenMetadata to Trino | OpenMetadata pods; the OLF-owned `openmetadata-trino-ingestion` Job and its hourly CronJob | Trino as user `openmetadata` | Trino `BasicAuth` over HTTPS as principal `openmetadata` when `trino_workload_authentication` is `password` (the default with `spec.access`); the Job verifies the HTTPS listener. Without it, a string match (read-only rules). OpenMetadata 1.13.6 pipeline-client pods could not receive the CA, which is why OLF owns the Job ([ADR 0016](../../adr/0016-trino-workload-authentication.md)) | All stage catalogs, read | `trino-credential-openmetadata` when workload authentication is `password`; none otherwise | Per [ADR 0016](../../adr/0016-trino-workload-authentication.md) |
| Polaris `om` principal | Nothing | n/a | A principal the Polaris bootstrap still creates with `CATALOG_MANAGE_CONTENT` on every stage catalog. OpenMetadata crawls through Trino, and no module or `olf` command reads its Secret ([ADR 0015](../../adr/0015-polaris-identity-provider-evaluation.md)) | All stage catalogs, write | `om_credentials_secret_name` | Dormant; removal is follow-up scope |
| OpenMetadata ingestion bot | Floe and Dagster pods, governed stages | OpenMetadata API (OpenLineage) | Bot JWT, sent as `OPENLINEAGE_API_KEY` / Floe `secret_name` | Governed stages only | `openmetadata-ingestion-bot`, key from the contract | The OpenMetadata bootstrap Job regenerates it on each run. **To verify** that the bot token keeps validating once OIDC is enabled (#25) |
| Dagster | Pods, `olf e2e`, and anyone who can reach the Service | Dagster webserver, ClusterIP port 80, and its GraphQL API | **No native authentication or per-user authorization.** None is configured in `infra/helm/values/local/dagster.yaml`, and Dagster OSS has none. The ingress route is protected by the perimeter (#176); this in-cluster path is protected by the stage namespace's default-deny NetworkPolicy, which admits only the stage's own namespace and Traefik ([ADR 0017](../../adr/0017-local-network-policy-and-rbac.md)), and by Kubernetes RBAC for `pods/portforward` and `exec`. See [Dagster boundary](#dagster-boundary) | n/a | None | n/a |
| Dagster run launcher | Dagster pod | Kubernetes API, creating run Jobs | The pod's ServiceAccount | Jobs and pods in its own stage namespace only, no Secret verbs: exception E3 in [ADR 0017](../../adr/0017-local-network-policy-and-rbac.md) | None | n/a |
| Issuer back-channel | oauth2-proxy, Superset, OpenMetadata pods | `https://auth.<base_domain>` through Traefik, trusting the local CA (`in-cluster-resolution` module) | Client secret at the token endpoint; JWKS is public | One client each | `dex-client-<consumer>` | Client rotation runbook |
| Datastores | Dex, Polaris, Superset, OpenMetadata | PostgreSQL | Per-service database user | Own database | `postgresql-*-creds` | Terraform-generated; residual debt |

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
`olf infra openmetadata deploy-metadata`, `olf infra report import` and the e2e assertions
reach the internal Service and never meet forwardAuth. Only the ingress drills
in `olf e2e` pass `through_ingress=True`.

**Superset and OpenMetadata callers.** Once those accept issuer login,
OpenMetadata's basic login answers 403, so `olf` and the bootstrap Job use the
admin bot `olf-automation`. Superset's REST `db` login is read from source to
survive; **to verify** on a running stack
([`identity-service-permissions.md`](identity-service-permissions.md)).

## Dagster boundary

Dagster OSS has no native authentication and no per-user authorization (#26).
The tree configures none. Three paths reach the same webserver and GraphQL API:

| Path | Who | What stops everyone else |
| --- | --- | --- |
| `https://dagster-<stage>.<base_domain>` through Traefik | A person | The perimeter (ADR 0014, seam 3): only `platform-admin` and `data-engineer` hold the `orchestration` grant in `release/identity-roles.yaml`; every other role and an unauthenticated request is refused before the request reaches Dagster. `olf infra e2e run --env local` asserts this per route (`e2e/_perimeter.py`) |
| `kubectl port-forward` to the `dagster-dagster-webserver` Service | `olf infra e2e run` (`e2e/_dagster.py`) and an operator | Kubernetes RBAC: `pods/portforward` in the stage namespace. The forward binds loopback, and the caller needs no session |
| The Service by its in-cluster name | Any pod | Default-deny NetworkPolicy: only Traefik and the stage's own namespace are allowed ([network policy and RBAC](identity-network-policy-and-rbac.md)) |

The two internal paths are class 2 (a workload or operator acting through
Kubernetes RBAC), not an authentication bypass that is reachable from outside
the cluster: the Service is `ClusterIP`, the only route out of the cluster is
the perimeter-protected ingress, and the kind ingress listens on `127.0.0.1`
(ADR 0013). The OpenMetadata Dagster connector is deferred
(`technical-debt.md`), so OpenMetadata does not call Dagster today.
The stage namespace's default-deny policy, not a probe, is what limits the
in-cluster path: the negative probes of `olf infra e2e run --env local`
(`e2e/_network.py`) cover PostgreSQL, Polaris, Trino, OpenMetadata and Dex, not
Dagster.

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
  authentication" in [`technical-debt.md`](../../technical-debt.md)). [ADR 0016](../../adr/0016-trino-workload-authentication.md)
  selects password-file authentication over HTTPS and proves a DEV credential
  cannot become PROD; no caller is migrated yet, so the rows above still
  describe the unauthenticated path.
- **Polaris keeps its own client credentials.** Whether the issuer could
  authenticate the catalog callers instead is evaluated in
  [ADR 0015](../../adr/0015-polaris-identity-provider-evaluation.md) and deferred.
- **Static `admin` password for Superset.** `olf infra report import` authenticates
  with it; OpenMetadata moved to the `olf-automation` bot.
- **Unattended automation with human SSO on** (ingestion, Superset to Trino,
  Floe and dbt to the catalog) is exercised by `olf infra e2e run --env local`.
