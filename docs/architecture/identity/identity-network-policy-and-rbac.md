# Network policy and RBAC

How the local stack limits which pods can reach which services and what each
ServiceAccount may do in the Kubernetes API. This is the control that keeps
class 2 paths from being used by anyone else (#46, #178). The binding rules are
in [ADR 0017](../../adr/0017-local-network-policy-and-rbac.md). The flows as
data are in `release/network-flows.yaml`, and the [flow table](#flow-table)
below explains each row; `olf check flows` requires every `id` in the file to
be a row of that table.

Scope: the local provider. AWS and Azure enforcement is outside it.

## The model

| Rule | Detail |
| --- | --- |
| Default deny | Ingress and egress are denied in every stage namespace (`olf-<stage>`). In `olf-system` only the workloads listed under `workloads` in the flow file (`var.workloads` of the policy module) are selected; the rest of `olf-system` stays unisolated. DNS is allowed. |
| One flow, one row | Every other flow is a row of `release/network-flows.yaml`, read by the policy module (`modules/security/network-policy`) and by `olf e2e`. `olf check flows` validates the file. For a pod that a policy selects, a flow that is not listed is denied. |
| Both sides | When both pods are selected by a policy, a flow needs a row on both sides: egress on the source, ingress on the destination. A side that no policy selects (such as an `olf-system` pod outside `workloads`) is not restricted on that side. |
| Stage namespace = one trust domain | Inside a stage namespace pods talk freely, because run pods and Floe Jobs are created with labels that user code controls. Between namespaces, and between `olf-system` workloads, every flow is an allow row. |
| Exceptions | `kube-system` is not a policy target: a default-deny there could take down DNS, kube-proxy or the CNI. |

Examples of what the rows deny: one stage reaching another stage's Dagster or
Superset, a stage namespace reaching Dex, PostgreSQL from outside its listed
sources, and any pod reaching Dagster except Traefik and the stage's own
namespace.

## Namespaces and selectors

| Namespace | Holds | Baseline | Isolation |
| --- | --- | --- | --- |
| `olf-system` | PostgreSQL, SeaweedFS (master, volume, filer, S3), Polaris, Dex, oauth2-proxy (one per role floor), the portal, the platform API, Traefik, cert-manager (controller, webhook, cainjector), OpenMetadata, OpenSearch, the log collector, Prometheus and kube-state-metrics when metrics are on, and the bootstrap Jobs and CronJobs | the listed workloads are denied both ways, with DNS | Per-workload allow rows; unlisted pods are unisolated |
| `olf-<stage>` | Dagster webserver, daemon, code server, run pods and Floe Jobs; Superset (web, worker, Redis, init Job); the stage's Trino coordinator; the stage Secrets | default-deny both ways, DNS, intra-namespace | The whole namespace is one trust domain |
| `kube-system` | CoreDNS (with the in-cluster-resolution Corefile), kube-proxy, kindnet | unmanaged | Exception |

Every policy selects pods by the labels the charts and modules already set.
Jobs carry `openlakeforge.io/job=<name>`. Selectors per workload (`name` is
`app.kubernetes.io/name`): PostgreSQL `name=postgresql`; SeaweedFS
`name=seaweedfs` plus `component`; Polaris `name=polaris`; Trino
`name=trino,component=coordinator`; Dex `name=dex`; Traefik `name=traefik`;
cert-manager `component` in `controller|webhook|cainjector`; OpenMetadata
`name=openmetadata`; OpenSearch `name=opensearch`; Superset `app=superset`,
`app=superset-worker`; Dagster `name=dagster` with `component` (webserver,
daemon) and `name=dagster-user-deployments`; the oauth2-proxy floors the
constant label `openlakeforge.io/workload=oauth2-proxy`, because their names
differ. A chart pod is matched with its `instance` label as well, so a Job that
shares its workload's `name` label is selected by its own
`openlakeforge.io/job` label instead. The stage namespaces are matched by their
`openlakeforge.io/stage` label and the shared one by
`openlakeforge.io/scope=shared`, not by name.

## Flow table

Each row is a pair of policies, egress on the source and ingress on the
destination, unless a side is unselected. Ports are container ports: a policy
sees the post-DNAT pod port. Whether a row is confirmed on a running cluster is
its `tag` in the flow file. Rows with no entry in the file are covered by the
stage rule (F8, F10, F16), rendered from the profile (F23, F34), or not policy
flows at all (F24, F25). F18, F22, F26 and F27 were removed with Keycloak and
are not reused.

| # | From | To | Port | Why |
| --- | --- | --- | --- | --- |
| F1 | any (the host, through kind's 80 and 443 mappings) | Traefik | 8443; 8000 in plain-HTTP mode (`tls: none`), allowed in both | `traefik.yaml`, `kind-cluster.yaml`; the hostPort passes through the CNI's portmap |
| F2 | Traefik | oauth2-proxy (every floor) | 4180 | forwardAuth address in `modules/access/perimeter` |
| F3 | Traefik | Dex 5556, portal 8080, platform API 8080, OpenMetadata 8585 (`olf-system`); Superset 8088 and Dagster webserver 80 (stage) | per route | `service_routes` and `ingress_routes` (the platform API's `/api` path) in `contracts.tf` |
| F4 | OpenMetadata; Superset's web pods (`app=superset`) in each stage namespace | Traefik | 8443, 8000 | the issuer's public URL through the CoreDNS rewrite: OpenMetadata's discovery and token requests, and Superset's sign-in and its per-user Trino token's exchange and refresh until #1092. No other stage pod calls a public route, so the stage side names Superset's web pods (rendered as `olf-flows-f4`), not the namespace; `olf check flows` rejects a row that opens Traefik to a whole stage namespace |
| F5 | every managed pod | CoreDNS in `kube-system` (`k8s-app=kube-dns`) | 53 UDP and TCP | every pod resolves; `*.<base_domain>` is rewritten to Traefik |
| F6 | Dex, Polaris, OpenMetadata, the Postgres bootstrap and Polaris metastore bootstrap Jobs; the stage namespaces (Superset, Dagster) | PostgreSQL | 5432 | `postgresql_contract`; `stage_databases`. The Postgres bootstrap Job's pods carry the Postgres labels, so they have their own row |
| F7 | Polaris | SeaweedFS S3 | 8333 | table metadata written to the warehouse bucket; not traced in the module |
| F8 | a stage's Trino coordinator | Polaris 8181; SeaweedFS S3 8333 | 8181, 8333 | `catalog_contract`, `storage_contract`; the coordinator runs in the stage namespace, so it takes the stage rows F9 |
| F9 | the stage namespaces (run pods, Floe Jobs) | Polaris 8181; SeaweedFS S3 8333 | 8181, 8333 | Floe and dbt; `AWS_ENDPOINT_URL_S3`, the S3 compute-log manager in `dagster.yaml` |
| F10 | a stage's own pods | its own Trino coordinator | 8080 | no row: a stage namespace is one trust domain, and no other stage reaches it. Trino has no authentication of its own (ADR 0016) |
| F11 | Traefik (the Trino UI), OpenMetadata | the stage namespaces (a stage's Trino) | 8080 | the OpenMetadata crawl; the OLF-owned ingestion Job runs beside the coordinator and needs no row |
| F12 | the stage namespaces of a governed stage | OpenMetadata | 8585 | `OPENLINEAGE_URL`, the ingestion Job |
| F13 | the role-sync CronJob; OpenMetadata itself | OpenMetadata | 8585 | `pipelineServiceClientConfig` (`metadataApiEndpoint`); the server's own Service, needed only if the CNI does not hairpin it |
| F14 | OpenMetadata | Superset (stage) | 8088 | `superset_url` registered by `olf`'s bootstrap |
| F15 | OpenMetadata | OpenSearch | 9200 | `elasticsearch.host` in `openmetadata.yaml` |
| F16 | Superset web, worker, init Job | Redis (stage) | 6379 | intra-namespace, no row |
| F17 | SeaweedFS pods among themselves; the bucket and retention Jobs | master 9333, 19333; filer 8888, 18888; volume 8080, 18080; S3 8333 | those | chart Services; the `bucket` and `object_retention` Jobs (both `openlakeforge.io/job=bucket-bootstrap`) wait on 9333 and 8888 and run `weed shell` |
| F19 | the Polaris metastore bootstrap Job | Polaris | 8181 | `metastore-bootstrap.sh.tftpl`: its first step probes the token endpoint |
| F20 | the credential Jobs (`dex-credentials`, `postgresql-credentials`, `perimeter-cookie-secret`, `publish-local-ca`); the stage namespaces (the Dagster daemon and run pods create Jobs); OpenMetadata (creates ingestion Jobs); Traefik; cert-manager (three, and the `startupapicheck` Job); the platform API (its service probes and TokenReviews) | kube-apiserver | 443 (Service) and 6443 (the endpoint) | the RBAC below; whether the Service address, the endpoint or both reach the policy depends on DNAT order, so both are allowed |
| F21 | the API server (control-plane node) | cert-manager webhook | 10250 | admission webhook; a wrong rule fails `terraform apply` |
| F23 | oauth2-proxy, the platform API | an `issuer: external` host | 443 | rendered from the profile; the platform API reads the issuer's discovery document and keys |
| F24 | kubelet (node) | any pod | probes | not a policy flow: node-to-local-pod traffic is not blocked |
| F25 | `kubectl port-forward`, `exec`, `logs` | any pod | none | the kubelet's stream enters the pod's own network namespace over loopback; `olf` reaches Polaris and OpenMetadata this way for their bootstraps |
| F28 | Dex | any address outside the pod and Service ranges | 443 | `github.com` and `api.github.com` at every login and refresh |
| F29 | Dex | the Dex e2e's stub GitHub pod (label `openlakeforge.io/e2e=github-stub`, #405) | 8443 | the connector `hostName` is `olf-e2e-github.olf-system.svc`; F28 excludes the pod and Service ranges |
| F30 | the role-sync CronJob (`openlakeforge.io/job=openmetadata-role-sync`) | any address outside the pod and Service ranges | 443 | team members from `api.github.com`; its calls to OpenMetadata ride F13 |
| F31 | oauth2-proxy; Polaris; the platform API; OpenMetadata | Dex | 5556 | the signing keys for people's tokens and the perimeter's code exchange, over Dex's Service (ADR 0013, decision 8) |
| F32 | the platform API | Dagster webserver 80 (stage); OpenMetadata 8585 | 80, 8585 | its read endpoints (ADR 0021): each stage's GraphQL URL (`dagster_graphql_urls`), and OpenMetadata with the caller's token (`openmetadata_url`). Dagster checks nothing, so this row and the permission matrix are its only controls |
| F33 | the platform API | SeaweedFS S3 | 8333 | archived logs with the read-only key `openlakeforge-platform-api-logs` (`Read` and `List` on each stage's `logs/k8s/` and `logs/v2/`); audit records with `openlakeforge-platform-api-audit` (`Write` on `audit/platform-api/`); the files explorer with `openlakeforge-platform-api-files`; the per-stage quality reader and the alert-state reader |
| F34 | Dagster run pods (`app.kubernetes.io/component=run_worker`) of a stage whose profile declares `spec.stages.<stage>.source_egress.ports` | any address outside the pod and Service ranges | the declared ports, TCP | Bronze sources reading an external API or database (#660); rendered as its own policy `olf-flows-f34` in that stage only |
| F35 | the platform API | Superset (stage) | 8088 | its report routes, as the service user `olf-platform-api`, never `admin` |
| F36 | the platform API | the stage namespaces (a stage's Trino) | 8080 | a person's queries with that person's token (`query_endpoints`), and the health probe's `GET /v1/info`; the API holds no Trino credential |
| F37 | the log collector | SeaweedFS S3 8333; kube-apiserver 443 and 6443 | 8333, 443, 6443 | archive format v2 (ADR 0022) with the write-only key `openlakeforge-log-collector`; the `kubernetes` filter's pod and namespace metadata |
| F38 | the alert evaluator (`openlakeforge.io/job=alert-evaluator`) | Dagster webserver (stage) | 80 | read-only GraphQL reads of each stage (ADR 0023) |
| F39 | the alert evaluator | SeaweedFS S3 | 8333 | key `openlakeforge-alerts`: `Read`, `Write` and `List` on `alerts/*`; `Read` and `List` on each stage's `run-artifacts/quality/*`; `Read` only on each stage's `ACTIVE.json` and `revisions/*` and on `project/revisions/*` (the alert policy) |
| F40 | the alert evaluator | any address outside the pod and Service ranges | 443 | the webhook receivers, HTTPS only |
| F41 | the alert evaluator | the alert e2e's stub receiver (label `openlakeforge.io/e2e=alert-receiver`, #1173) | 8443 | F40 excludes the pod and Service ranges. The e2e also puts a temporary policy beside the stub that admits only the evaluator |
| F42 | the platform API | the log collector's spool probe (`log-collector-probe`) | 2021 | the oldest spooled file's age and the spool's size per stage (ADR 0022); no log text |
| F43 | the platform API | Polaris's management port (`polaris-mgmt`) | 8182 | `GET /q/health/ready` with no credential; the REST API on 8181 stays closed to the platform API |
| F44 | the alert evaluator | the platform API (`platform-api.olf-system:8080`) | 8080 | each stage's services route (ADR 0023), with a projected ServiceAccount token the API checks by TokenReview over its F20 egress |
| F45 | the platform API | Prometheus | 9090 | `query` and `query_range` for the named series of ADR 0024; Prometheus has no authentication, so this is its one way in. F45 to F51 render only when the profile enables metrics |
| F46 | Prometheus | kube-apiserver | 443, 6443 | node discovery with its ServiceAccount token |
| F47 | Prometheus | the nodes (`/32` addresses) | 10250 | the kubelet's `/metrics/resource` and `/metrics` |
| F48 | Prometheus | the stage namespaces (a stage's Trino) | 8080 | `/metrics` as the user `olf-metrics`, no password |
| F49 | Prometheus | SeaweedFS master, volume, filer and S3 | 9327 | `/metrics` of each process (`-metricsPort=9327`); F17 does not open it |
| F50 | Prometheus | kube-state-metrics | 8080 | `/metrics`; the telemetry port 8081 has no row |
| F51 | kube-state-metrics | kube-apiserver | 443, 6443 | `list` and `watch` of pods and workload controllers in the OLF namespaces |
| F52 | the quality reconciler (`openlakeforge.io/job=quality-reconciler`, one CronJob per governed stage) | OpenMetadata | 8585 | test definitions, cases and results (ADR 0007), with the stage's quality-writer bot token |
| F53 | the quality reconciler | SeaweedFS S3 | 8333 | key `openlakeforge-quality-sync-<stage>`: `Read` and `List` on the stage's `run-artifacts/quality/*`; `Read`, `Write` and `List` on its `quality-sync/*` |

Everything not in the table is denied: notably stage to stage, stage to Dex,
PostgreSQL from outside the F6 sources, the SeaweedFS master, filer and volume
ports from outside, Polaris's 8182 from anything but the platform API, Trino
from an unlisted namespace, and any pod to Dagster except Traefik, the
platform API, the alert evaluator and the stage's own namespace.

Not read from the tree: the labels of OpenMetadata's pipeline-client ingestion
pods (set by the server at runtime, so they are denied towards Trino); whether
Polaris calls SeaweedFS (F7); and the DNAT order, which is why F20 allows both
addresses.

## Enforcement is checked, not assumed

`olf infra deploy --provider local` runs a canary after the foundation. It proves a
client and server pair connect with no policy, then requires an ingress-only
and an egress-only deny to block it. Last, an ingress deny must block, adding a
rule that allows the client must let it connect, and removing that rule must
block it again. The deploy fails if the cluster does not enforce NetworkPolicy
or does not admit what an allow rule permits. The canary runs in the deploy
only. `olf e2e` runs `check_network_and_rbac` (flow probes and service-account RBAC checks) in the
full suite, after the catalog, pipeline, restart-recovery and certificate
checks. One of its probes pairs F4's stage labels, which must reach Traefik,
with a pod carrying the run-pod label `app.kubernetes.io/component=run_worker`,
which must not.
The local kind cluster's default CNI (kindnet) passed on the pinned kind
version, so no other CNI is installed.

## Dex and GitHub egress

Dex calls `github.com` and `api.github.com` at every sign-in and refresh, and
the role-sync CronJob calls the GitHub API every 5 minutes. NetworkPolicy has
no hostname selector, so the rules allow port 443 to any address outside the
pod and Service ranges, on those pods only.

| Flow | From | To | Port |
| --- | --- | --- | --- |
| F28 | Dex pods | Any address outside the cluster's pod and Service ranges | 443 |
| F30 | The `openmetadata-role-sync` CronJob | Any address outside the cluster's pod and Service ranges | 443 |
| F29 | Dex pods | The stub GitHub pod of the identity e2e (label `openlakeforge.io/e2e=github-stub`) | 8443 |
| F23 | oauth2-proxy | An `issuer: external` host | 443 |

F29 exists only so `olf e2e` can sign in against a stub instead of real
GitHub; F28 excludes the pod and Service ranges, so the stub needs its own
row. F23 is rendered from the profile when `issuer: external` is set. The
cluster-side behaviour of F23, F28, F29 and F30 is marked as to-verify in the
flow file.

With `spec.identity.github.host` set to a GitHub Enterprise Server, Dex and the
role sync reach that host over the same port-443 rule.

## Source egress from run pods

A Bronze source that reads an external API or database runs in the Dagster run
pod of its product job. Run pods get no egress outside the cluster unless the
stage's Deployment Profile declares the ports its sources need:

```yaml
spec:
  stages:
    dev:
      source_egress:
        ports: [443, 5432]
```

| Flow | From | To | Port |
| --- | --- | --- | --- |
| F34 | Run pods (`app.kubernetes.io/component=run_worker`) of that stage | Any address outside the cluster's pod and Service ranges | The declared ports, TCP |

The rule is its own policy, `olf-flows-f34`, in that stage's namespace only;
the other pods of the namespace and the other stages get nothing. Because
stage pods carry labels user code controls, the label narrows the grant
without isolating it (ADR 0017, D10). The cluster-side behaviour of F34
is marked as to-verify.

## RBAC

The kind kubeconfig is cluster-admin, and the operator and CI are trusted. No
break-glass Role exists in the repository: `olf infra break-glass` uses the
operator's own kubeconfig
([identity-sessions-and-recovery.md](identity-sessions-and-recovery.md)).
Inside the cluster the Roles are narrowed where the platform owns them and the
rest are the exceptions ADR 0017 names (D5):

| Exception | Subject | Why it stays |
| --- | --- | --- |
| E1 | Create-if-missing credential Jobs (Dex credentials, perimeter cookie, PostgreSQL, Superset, SeaweedFS, the platform API's Superset users and query signing key, the local CA, Polaris, OpenMetadata) | `create` on Secrets or ConfigMaps cannot be limited by `resourceNames`; `get`, `update`, `patch` and `delete` on Secrets are pinned to named objects. The local CA publisher is the exception for ConfigMaps: it holds `get`, `patch` and `update` on all ConfigMaps in each trust namespace, and only its Secret read is pinned |
| E2 | OpenMetadata server and ingestion (chart Role) | Pod, Job and Secret verbs in `olf-system`, which its UI pipeline client needs; the widest grant |
| E3 | Dagster run launcher, per stage | Creates run Jobs and pods; no Secret verbs, but a stage's user code already holds the stage's Secrets |
| E4 | Traefik | Reads TLS Secrets in all namespaces |
| E5 | cert-manager | Issues certificates, with Secret verbs cluster-wide |
| E6 | Withdrawn (2026-11-23) | It named Calico's components, which were never installed |

### Inventory

| Subject | Grant | Used for | Verdict |
| --- | --- | --- | --- |
| `default` ServiceAccount, every namespace | no Role | a token mounted into pods that do not opt out | Authorizes nothing beyond discovery; turning off the automount everywhere is deferred hardening |
| `dex-credentials`, `<perimeter>-cookie`, `postgresql-credentials`, `superset-credentials`, `seaweedfs-credentials`, `platform-api-superset-users`, `platform-api-query-signing-key` | `create` Secrets (namespace-wide) and `get` by `resourceNames` | create-if-missing credentials | E1. The pinned names are `E1_PINS` in `tools/olf/olf/infra/e2e/_network.py`. `seaweedfs-credentials` also patches `seaweedfs-s3-auth` and the S3 Deployment by name in `olf-system`; `platform-api-superset-users` mints each stage's password in `olf-system` and copies it into that stage's namespace |
| `publish-local-ca` (each trust namespace) | ConfigMaps `create/get/patch/update` (namespace-wide); `get` on the CA Secret by name | publishes `olf-local-ca` | E1 (ConfigMaps) |
| `polaris-bootstrap` (`olf-system`) | Secrets `create`, namespace-wide; `get` on `polaris-token-broker` and `polaris-bootstrap-credentials` | the token-key Job | Narrowed (ADR 0017, D8). The Polaris bootstrap itself runs in `olf` with the operator's kubeconfig (#562) |
| `openmetadata-bootstrap` (`olf-system`) | Secrets `create`, namespace-wide | the fernet-key Job's `openmetadata-fernet-key` | Narrowed (D8). The OpenMetadata bootstrap runs in `olf` (#562) |
| OpenMetadata server and `openmetadata-ingestion` (chart `k8s-pipeline-rbac.yaml`) | pods, ConfigMaps, Secrets `get/list/create/update/patch/delete`, Jobs, CronJobs, `omjobs`, namespace-wide in `olf-system` | ingestion pipelines as Jobs | E2. A workload it creates can mount any Secret in `olf-system` and run as any ServiceAccount there, including Traefik's and cert-manager's |
| `dagster`, `dagster-user-deployments` (stage) | Jobs and pods `create/delete/get/list/patch/update/watch`, pods `log/status`, events | run launcher, Floe Jobs | E3 (also in [identity-callers.md](identity-callers.md)) |
| `platform-api` (a Role and RoleBinding in each stage namespace, once `probe_namespaces` is set) | pods, Deployments and StatefulSets `list` | the service probes (ADR 0021) | Read-only; nothing in `olf-system`. With `service_callers` set it also holds the ClusterRole `olf-system-platform-api-token-review` (`tokenreviews` `create` only). Not an exception |
| `log-collector` (ClusterRole `olf-system-log-collector`) | pods and namespaces `get/list/watch`, cluster-wide | the `kubernetes` filter's metadata (ADR 0022) | Read-only; no `nodes/proxy`, Secret, `pods/log` or `pods/exec`. Not an exception |
| `prometheus` (ClusterRole `olf-system-prometheus`, with metrics on) | `nodes` `get/list/watch`; `nodes/metrics` `get` | node discovery and the kubelet scrape (ADR 0024) | Read-only; no `nodes/proxy`. Its token is projected for 600 seconds; on kind the kubelet job skips certificate verification. Not an exception |
| `kube-state-metrics` (a Role in `olf-system` and each stage namespace, with metrics on) | pods, Deployments, StatefulSets, DaemonSets `list/watch` | the restart, readiness and replica series (ADR 0024) | Read-only and namespaced; the module refuses `kube-system`. Not an exception |
| `traefik` ClusterRole | Secrets `get/list/watch` in all namespaces; Ingress status | TLS Secrets | E4 |
| cert-manager ClusterRoles | Secrets `get/list/watch/create/update/delete` cluster-wide; pods and Services for HTTP-01 solvers; the webhook's namespaced `dynamic-serving` Role | issuing certificates | E5 |
| SeaweedFS ClusterRole `seaweedfs-rw-cr` | not created (`global.seaweedfs.createClusterRole: false`) | the chart's master-restart hack | Removed (D8); the e2e asserts it is absent |
| `olf` operator and CI | cluster-admin | everything | Trusted |

### What the e2e asserts

`kubectl auth can-i --as=system:serviceaccount:<ns>:<sa>` (impersonation by
the admin kubeconfig) must answer no: for the `default` ServiceAccount of
`olf-system` and of every stage namespace, `get` and `list` Secrets, `create`
pods and `create` rolebindings; for each module ServiceAccount, `get` on any
Secret outside its `resourceNames`; for every subject, `create` ServiceAccount
tokens, `escalate` or `bind` Roles, and `patch` ConfigMap `coredns` in
`kube-system`. A second assertion lists every Role and ClusterRole bound to a
ServiceAccount in the OLF namespaces that grants a Secret verb, or `create` on
pods, Jobs, CronJobs or workload controllers, and fails on any not covered by
E1 to E5. The comparison is by role kind, role, subject and resources, not by
name prefix; bindings to the `system:serviceaccounts[:<ns>]` groups count for
every ServiceAccount in the namespace.

## Limits

- Anything running in a stage namespace can reach everything that namespace may
  reach.
- Any pod allowed to reach Traefik can request the Dex route, `auth.<base_domain>`,
  because the network cannot name a path. In a stage namespace flow F4 lets
  only Superset's web pods (`app=superset`) reach Traefik, for their calls to
  the issuer's public URL; Dagster and run pods cannot, and `olf e2e` checks
  that a pod with the run-pod label is dropped. User code that sets
  `app=superset` on a pod still gets through: the label narrows, it does not
  isolate (ADR 0017, D10).
- kindnet programs a new or restarted pod's policy asynchronously: a fresh pod
  briefly has unrestricted egress, and a restarted destination is briefly
  unreachable after it reports Ready. The e2e retries around it (60 seconds
  after the Polaris restart drill); the window is not measured.
- `kubectl port-forward`, `exec` and `logs` are RBAC paths, not network paths,
  and policy does not apply to them. A cluster administrator stays trusted.
- Resource limits, Pod Security Standards, hostname-based egress filtering,
  service meshes and AWS or Azure enforcement are not covered.

Callers and credentials these rules protect:
[identity-callers.md](identity-callers.md),
[identity-classes.md](identity-classes.md).
