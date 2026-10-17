# ADR 0017: Local default-deny NetworkPolicy, namespace RBAC and the enforcing CNI

## Status

Design for #46 (local qualification only); slice 1a (the canary gate), slice 2
(the flow file, its check, the policy module and the stage-namespace policies),
slice 3a (default-deny on the `olf-system` data plane: Postgres, SeaweedFS,
Polaris, Trino) and slice 3b (the access plane: Traefik, oauth2-proxy, the
issuer, the portal, cert-manager, OpenMetadata, OpenSearch and the Jobs) are
built; the cluster-side behaviour of the 3b rows is **[T]**. What binds today
is the policy model, the flow table, the RBAC inventory, the conformance plan
and the slice order. The CNI question is
**settled: Option A, kindnet.** The canary passed on the pinned kind 0.32.0
(History), so Calico (slice 1b) is not built.

Claims carry a tag. **[V]** read from this repository or rendered from the
pinned charts with `helm template` (no cluster). **[D]** read in upstream
documentation, not run. **[T]** to verify on a running cluster; no cluster was
touched while writing this.

## Context

No `NetworkPolicy` object exists in the tree [V]. Every pod reaches every
Service: Postgres (per-service passwords only), Polaris, SeaweedFS, Keycloak's
whole HTTP surface, Dagster's unauthenticated GraphQL API, and Trino (which
trusts `X-Trino-User`, [ADR 0016](0016-trino-workload-authentication.md)). The
perimeter ([ADR 0014](0014-identity-and-authorization.md)) protects ingress
only, so an untrusted pod bypasses it. #178 makes the callers class 2 and
#46 is the control that stops anyone else using those paths. Cluster
administrators stay trusted; `kubectl port-forward` and `exec` are RBAC, not
network, paths.

Two facts about the tree shape the model. Traefik and cert-manager are **not**
in namespaces of their own: both are Helm releases in the shared namespace
`olf-system` [V: `modules/access/traefik`, `cert-manager`]. Stage namespaces
(`olf-<stage>`) hold Dagster, its run pods and Floe Jobs, and Superset
[V: `environments/local/main.tf`].

## Premise check: does kindnet enforce?

The request for #46 assumes kind's default CNI, kindnet, does not enforce
NetworkPolicy, and the maintainer approved Calico on that basis. The kind
v0.24.0 release notes say "Out-of-the-box support for network policy via
sigs.k8s.io/kube-network-policies" [D], and kindnet's own documentation says it
implements NetworkPolicy that way, by inspecting the first packet of a
connection and programming the host dataplane [D]. The tree pins kind 0.32.0
[V: `release/component-catalog.yaml`] and its `kind-cluster.yaml` neither
disables the default CNI nor pins a node image, so the CNI is whatever
kindnetd that kind release ships. The upstream project calls itself a
"reference implementation" [D]; its semantics for `ipBlock`, host-network
sources (kubelet, the API server) and Service DNAT ordering are **[T]**.

The two options, and why slice 1 is a gate (see [Decisions](#decisions)):

| | A. kindnet (kind 0.32.0 default) | B. Calico (fallback) |
| --- | --- | --- |
| Install work | None. Slice 1 is the enforcement canary | A CNI install in the foundation, ~450 lines (below) |
| Existing local clusters | Work as they are | Must be destroyed and recreated: the CNI cannot be swapped under a running kind cluster |
| Cost per developer | None | Extra RAM, extra startup minutes, a second upgrade surface (Calico release, Kubernetes-version-specific CRDs [D]) |
| Semantics | Reference implementation, [T] | The mature, documented implementation |
| Decided by | A canary that proves a deny-all policy drops traffic | Only if the canary fails: Calico is the documented fallback |

**Slice 1a, the canary on the pinned kind 0.32.0, is the gate.** If it blocks,
kindnet is the dataplane, the kindnet version goes in the History and slice 1b
is not built. If it does not enforce, B is built as designed below. The rest of this ADR holds under either; only the CNI section
and the apiserver rows of the flow table are CNI-sensitive.

## Decision

1. **Dataplane gate.** `olf deploy` fails before any policy is applied unless
   the cluster enforces policy, and the e2e re-proves it with a canary on every
   run. Policy objects without enforcement never count as done (#46).
2. **Default-deny ingress and egress** in `olf-system` and every stage
   namespace, DNS allowed, every other flow an explicit row in one file,
   `release/network-flows.yaml`, read by Terraform and by `olf e2e` (the
   `release/identity-roles.yaml` precedent [V]).
3. **A stage namespace is one trust domain; `olf-system` is not.** Inside a
   stage namespace pods talk freely: run pods and Floe Jobs are launched by the
   `dagster` ServiceAccount with labels user code controls, so a label is not a
   boundary there. Between namespaces, and between `olf-system` workloads,
   every flow is an allow row selecting on chart-native labels.
4. **Stage-first rollout.** Pods in a namespace with no policy are not
   isolated. Policies go onto the stage namespaces first, whose destinations in
   `olf-system` are still open, then onto `olf-system`; the stack works after
   every slice.
5. **RBAC is inventoried, narrowed where the roles are ours, and pinned** by an
   e2e check that fails when a new Secret-reading binding appears.
6. `kube-system`, `calico-system`/`tigera-operator` (Option B) and the
   control-plane host network are **not** policy targets: a default-deny there
   can take the cluster down (CoreDNS, kube-proxy, the CNI itself). They are
   the documented exception, and the egress rows below allow only what the OLF
   pods need from them.

## The enforcing CNI

### Option B: Calico

| Topic | Design |
| --- | --- |
| Cluster config | `infra/kind/local/kind-cluster.yaml` gains `networking: {disableDefaultCNI: true, podSubnet: 10.244.0.0/16, serviceSubnet: 10.96.0.0/16}` (kind's own defaults, written down so the Calico pool can name them). The control-plane node keeps the `ingress-ready` label and 80/443 mappings |
| Ordering | Nodes stay `NotReady` and CoreDNS `Pending` until a CNI exists [D], and the foundation runs `kind create cluster --wait 120s` [V], which would fail. The foundation passes `--wait 0s` (`kind_wait_timeout`), then `olf` installs the CNI in `foundation_up`, then waits for nodes. Terraform keeps owning cluster creation; the CNI release is not in Terraform state, because the foundation root has no Kubernetes or Helm provider and the kubeconfig does not exist at plan time |
| Pins | Chart `tigera-operator` v3.33.0 (appVersion v3.33.0; archive sha256 `efcc8633742c136708cd8de9673862c6eee3a91ed176d7f2835690ab625ad846` as downloaded from `https://docs.tigera.io/calico/charts` while writing) under `components.helm.charts` in the catalog, fetched by the existing `CatalogChart`/`prepare_chart` cache. Images under `components.images` with digests: the operator (`quay.io/tigera/operator:v1.44.0`, the chart default [V]) and `quay.io/calico/{node,cni,kube-controllers,typha}:v3.33.0` [D]. Digests are recorded by the implementing PR, not here [T]; `olf check components` enforces them. Calico's own images are pinned through an `ImageSet` named `calico-v3.33.0` [D] |
| Install | `helm upgrade --install calico <cached chart> -n tigera-operator --create-namespace -f infra/helm/values/local/calico.yaml` with `installation.enabled: false`, `apiServer`, `goldmane` and `whisker` disabled (the chart enables all three by default [V]; none is needed for Kubernetes policy). Then `kubectl apply` of `infra/kind/local/calico-installation.yaml`: an `Installation` with one IP pool `10.244.0.0/16`, `VXLANCrossSubnet` (upstream's kind guide uses a VXLAN overlay [D]; the mode, and whether it works on WSL2, is **T**), `controlPlaneReplicas: 1`, `kubeletVolumePluginPath: None`. The CR is a separate step because the chart ships no `crds/` directory (the operator bootstraps its own CRDs at start [V]), so a CR in the same release may not map, the reason the repo already splits `local-ca` from cert-manager [V] |
| Prefetch | The images load into every node with the `prefetch.py` mechanism (`docker pull`, `save`, `ctr import`) before the install, so three nodes do not each pull from quay.io (rate limits, CI time). `_prefetch_one_image` becomes public; the list is the catalog images above |
| Readiness gate | `kubectl wait tigerastatus/calico --for=condition=Available`, every node `Ready`, CoreDNS available; then the canary of the next section. Runs on **every** `olf deploy`, not only on creation, so a cluster whose dataplane vanished cannot take policies silently |
| Existing cluster | `foundation_up` finds the cluster and reuses it ("already exists. Reusing it", `foundation/local-kind/main.tf` [V]); a changed config file hash does not recreate it. `olf` therefore detects a cluster without the CNI (`kindnet` DaemonSet present or `tigerastatus` absent) and stops: `olf destroy --provider local`, then deploy. Local data is lost; no production deployments exist (maintainer note, pre-beta) |
| Destroy | Deleting the cluster deletes the CNI. `olf destroy --phase platform` leaves the cluster and the CNI; the `tigera-operator` and `calico-system` namespaces carry no `openlakeforge.io/managed-by` label, so the foundation guard that refuses to delete a cluster with platform namespaces ignores them [V: `foundation_down` selects by profile label] |
| Upgrade | Change the pins and recreate the cluster. In-place operator upgrades are not supported or tested: local clusters are disposable |
| Cost | **Not measured [T]; measure before merging slice 1b** with `docker stats` on the node containers before and after, and record startup time of `olf deploy --phase foundation`. Expected from the objects: one operator pod, a `calico-node` DaemonSet pod per node (3), `calico-kube-controllers`, possibly Typha; five images pulled once per node unless prefetched. The first cost every developer sees is the forced destroy and the CI runner's longer foundation step |
| What changes for developers | The first deploy after the change recreates the cluster; pods no longer reach arbitrary Services, so an ad hoc `kubectl run curl` in an OLF namespace is blocked (use `port-forward`, or `exec` into an allowed pod); `olf e2e` creates and removes its own temporary policies; `kubectl get pods -A` shows two more namespaces |

Slice 1b must resolve these review findings before it is built; none is
designed here, because Option B is built only if the canary fails:

- **Guard before Terraform.** `triggers_replace` hashes the kind config, so the
  foundation apply replaces the resource and its destroy provisioner deletes the
  cluster; the missing-CNI check must run before that apply, or ownership of
  replacement must change.
- **Ownership.** The Calico release and `Installation` are static foundation
  infrastructure; place them in Terraform state, or rewrite ADR 0002 to allow
  an `olf`-owned step, rather than a second unrecorded ownership model.
- **CRDs.** The v3.33 operator chart may not install the Calico CRDs (the
  upstream kind guide installs them first, in a Kubernetes-version-specific
  file [D]); pin and apply the matching CRD bundle before the operator.
- **Image set.** v3.33 may have consolidated `cni`, `kube-controllers` and
  `typha` into `calico/calico`; derive the prefetch and `ImageSet` lists from
  the pinned operator's `--print-images=list` output, not from the names above.

### Option A: kindnet

Nothing is installed. Slice 1 is the gate and canary below, run against the
cluster `olf` already creates. The destroy-and-recreate step disappears
unless a developer's cluster predates kind 0.24 [D].

### The gate and the canary

`olf` creates a throwaway namespace with a deny-all policy, a server pod and a
client pod, and requires every connection attempt to fail, by timeout or by
refusal (the NetworkPolicy contract promises only that the connection cannot
occur, not how it is signalled [D]), while an identical pair in a second,
policy-free namespace connects, which also proves the server was listening. Deploy runs it once after the CNI is
ready; the e2e runs it again as its first step. This is the evidence for
"Local runtime demonstrably enforces default-deny network policy".

## Namespaces and the policy model

| Namespace | Holds [V] | Baseline | Isolation level |
| --- | --- | --- | --- |
| `olf-system` | PostgreSQL, SeaweedFS (master, volume, filer, S3), Polaris, Trino, Keycloak, oauth2-proxy (one per role floor), portal, Traefik, cert-manager (controller, webhook, cainjector), OpenMetadata, OpenSearch, OpenMetadata ingestion Jobs, every bootstrap Job | default-deny both ways, DNS | Per-service allow rows |
| `olf-<stage>` | Dagster webserver, daemon, code server, run pods and Floe Jobs; Superset (web, worker, Redis, init Job); the stage Secrets | default-deny both ways, DNS, intra-namespace | Whole namespace is one trust domain |
| `kube-system` | CoreDNS (with the in-cluster-resolution Corefile), kube-proxy, kindnet if Option A | unmanaged | Exception |
| `calico-system`, `tigera-operator` | the CNI (Option B) | unmanaged | Exception |

Every policy selects pods by the labels the charts and modules already set, so
no chart value changes in the policy slices. Jobs carry
`openlakeforge.io/job=<name>` [V: twelve Jobs and CronJobs]. Selectors per
workload (`name` is `app.kubernetes.io/name`): PostgreSQL `app.kubernetes.io/name=postgresql`; SeaweedFS
`app.kubernetes.io/name=seaweedfs` plus `component`; Polaris
`name=polaris`; Trino `name=trino,component=coordinator`; Keycloak
`name=keycloak`; Traefik `name=traefik`; cert-manager `component` in
`controller|webhook|cainjector`; OpenMetadata `name=openmetadata`; OpenSearch
`name=opensearch`; Superset `app=superset`, `app=superset-worker`;
Dagster `name=dagster` with `component` (webserver, daemon) and
`name=dagster-user-deployments` [V: `helm template` of the pinned charts].
The stage namespaces are matched by their `openlakeforge.io/stage` label and
the shared one by `openlakeforge.io/scope=shared` [V: `main.tf`], not by name.

## Flow table

Each row is a pair of policies: egress on the source, ingress on the
destination, unless a side's namespace is unmanaged. "Source" is the row of
[`identity-callers.md`](../architecture/identity-callers.md) or the file the
flow was read from. Ports are container ports (a policy sees the post-DNAT pod
port [D]).

| # | From | To | Port | Source | Tag |
| --- | --- | --- | --- | --- | --- |
| F1 | any (host, via the kind 80/443 mapping) | Traefik pod | 8443 (443 hostPort); 8000 not allowed | `traefik.yaml`, `kind-cluster.yaml`; the chart exposes `web` 8000 and `websecure` 8443 | V; hostPort through the CNI's portmap **T** |
| F2 | Traefik | oauth2-proxy (every floor) | 4180 | forwardAuth address in `modules/access/perimeter` | V |
| F3 | Traefik | Keycloak, portal, OpenMetadata, Trino (`olf-system`); Superset 8088 and Dagster webserver 80 (stage) | per route | `service_routes` in `contracts.tf` | V |
| F4 | oauth2-proxy | Traefik 8443 | 8443 | issuer discovery at `https://auth.<base_domain>` through the CoreDNS rewrite | V; Superset and OpenMetadata join when #25 lands |
| F5 | all managed pods | CoreDNS in `kube-system` (`k8s-app=kube-dns`) | 53 UDP/TCP | every pod resolves; `*.<base_domain>` is rewritten to Traefik | V |
| F6 | Keycloak, Dex, Polaris, OpenMetadata, Postgres bootstrap, Polaris metastore bootstrap; Superset and Dagster (stage) | PostgreSQL | 5432 | `postgresql_contract`; `stage_databases` namespaces | V |
| F7 | Polaris | SeaweedFS S3 | 8333 | table metadata is written to the warehouse bucket; not traced in the module | **T** |
| F8 | Trino | Polaris 8181; SeaweedFS S3 8333 | 8181, 8333 | `catalog_contract`, `storage_contract` | V |
| F9 | stage namespace (run pods, Floe Jobs) | Polaris 8181; SeaweedFS S3 8333 | 8181, 8333 | Floe/dbt rows; `AWS_ENDPOINT_URL_S3`, S3 compute-log manager in `dagster.yaml` | V (Floe runner labels **T**) |
| F10 | stage namespace | Trino | 8080 now; 8443 at ADR 0016 step 4 | Superset, dbt rows. The 8080 allow is removed in the same change that enables `PASSWORD`; kubelet probes use 8080 from the node and are unaffected | V |
| F11 | Traefik (the Trino UI), OpenMetadata, and the OLF-owned ingestion Job (`openlakeforge.io/job=openmetadata-trino-ingestion`, #410) | Trino 8080, 8443 | 8080, 8443 | OpenMetadata to Trino row | V (the Job's label is defined here, **T** until #410 creates it) |
| F12 | stage namespace of a governed stage | OpenMetadata | 8585 | `OPENLINEAGE_URL`, ingestion-bot token | V |
| F13 | OpenMetadata ingestion Jobs, bootstrap and refresh Jobs | OpenMetadata | 8585 | `pipelineServiceClientConfig` (`metadataApiEndpoint`); `openlakeforge.io/job` | V |
| F14 | OpenMetadata ingestion Jobs, bootstrap Job | Superset (stage) | 8088 | `superset_url` registered by the bootstrap | V; whether ingestion runs today **T** |
| F15 | OpenMetadata | OpenSearch | 9200 | `elasticsearch.host` in `openmetadata.yaml` | V |
| F16 | Superset web, worker, init Job | Redis (stage) | 6379 | intra-namespace | V |
| F17 | SeaweedFS pods among themselves; bucket Job | master 9333, 19333; filer 8888, 18888; volume 8080, 18080; S3 8333 | those | chart Services; `bucket` Job waits on 9333 and 8888 | V |
| F18 | Keycloak realm Job; `olf e2e run` (port-forward) | Keycloak | 8080 | `keycloak-config-cli` URL in `modules/identity/keycloak` | V |
| F19 | Polaris bootstrap and replication Jobs | Polaris | 8181 | `bootstrap.sh.tftpl` | V |
| F20 | Jobs: `keycloak-credentials`, `perimeter-cookie-secret`, `publish-local-ca`, `catalog-bootstrap`, `catalog-credential-replication`, `governance-bootstrap`, `governance-credential-replication`; Dagster daemon and run pods (Floe's runner creates Jobs); OpenMetadata server (creates ingestion Jobs); Traefik; cert-manager (three, and the `startupapicheck` post-install Job, label `component=startupapicheck`); the stage log archiver (`openlakeforge-k8s-log-archive`, `dagster` token, lists pods and reads logs [V: `log_archive.tf`, `libs/k8s_log_archive.py`]) | kube-apiserver | 443 (Service) and 6443 (the endpoint) | RBAC section | V |
| F21 | API server (control-plane node) | cert-manager webhook | 10250 | admission webhook | node-sourced traffic **T** |
| F22 | Keycloak | the SMTP host and port of `spec.identity.smtp` | profile-driven | rendered from the profile; `ipBlock` cannot name a host, so `0.0.0.0/0` minus the pod and Service CIDRs on that port | T |
| F23 | oauth2-proxy and, later, OIDC clients | an `issuer: external` host | 443 | profile-driven, same rendering | T |
| F24 | kubelet (node) | any pod | probes | not a policy flow: node-to-local-pod traffic is not blocked [D] | T |
| F25 | `kubectl port-forward`, `exec`, `logs` | any pod | none | the kubelet's runtime stream enters the pod's own network namespace, loopback, never crossing the policy hooks [D] | T |
| F26 | `olf e2e` issuer probe pod in `olf-system` (label `openlakeforge.io/e2e=issuer-probe`) | Traefik 8443 | 8443 | `check_discovery_from_pod` creates the pod there [V] | V; a flow-file row keyed on the label the e2e sets |
| F27 | Keycloak | the e2e mail-sink pod (`olf-system`, label `openlakeforge.io/e2e=mail-sink`) | 1025 | `mail_sink` points the realm's SMTP at the sink; the sink only receives [V: `_identity_mail.py`] | V; a flow-file row keyed on the label the e2e sets |
| F28 | Dex | any address outside the pod and Service ranges | 443 | Dex calls `github.com` and `api.github.com` at every login and refresh ([ADR 0014](0014-identity-and-authorization.md), "Network"); NetworkPolicy has no FQDN selector, so the rule is the port on any address, on the Dex pods only | T |
| F29 | Dex | the Dex e2e's stub GitHub pod (`olf-system`, label `openlakeforge.io/e2e=github-stub`, #405) | 8443 | the connector `hostName` is `olf-e2e-github.olf-system.svc`; F28 excludes pod and Service ranges, so the stub needs its own row (post-DNAT port) | T; a flow-file row keyed on the label the e2e sets |

Not derivable from source, to settle in the slice that touches each:
the labels of OpenMetadata ingestion Job pods (set by the server at runtime);
the labels and egress of Floe's runner Jobs (`floe_dagster.kubernetes_runner`
is in the image, not the tree) and of Dagster run pods; whether Polaris calls
SeaweedFS (F7); the ordering of Service DNAT and policy evaluation in the
chosen CNI, which decides whether F20 needs the ClusterIP, the endpoint, or
both (the plan allows both); whether any chart pod needs internet egress
(none is expected; images are pulled by the node, not the pod).

Denied by construction, and tested (negative matrix below): everything not in
the table. Notably stage to stage (DEV cannot reach PROD's Dagster or
Superset), stage to Keycloak, Postgres from outside the F6 sources, SeaweedFS
master, filer and volume ports from outside, Polaris's management port 8182
from anywhere, Trino from an unlisted namespace, and any pod to Dagster except
Traefik and the stage's own namespace.

**Known leak the network cannot close.** Any pod allowed to reach Traefik
(F4 sources) can request `https://auth.<base_domain>`, which routes the whole
Keycloak service including the admin console ([ADR 0014](0014-identity-and-authorization.md),
"Open"). Dagster and run pods are not given F4, so user code cannot; narrowing
the route is the follow-up that ADR already names.

## RBAC inventory

Rows are what the tree grants today. "Tighten" is within #46; "exception" is
recorded and pinned by the e2e check.

| Subject | Grant | Used for | Verdict |
| --- | --- | --- | --- |
| `default` ServiceAccount, every namespace | no Role [V] | token auto-mounted into Postgres, Keycloak, portal, oauth2-proxy, Trino and Superset pods; only `log_archive.tf` sets `automount_service_account_token` [V] | Tighten: `automount_service_account_token = false` on the module-owned workloads that never call the API. Chart-owned pods: **T** |
| `keycloak-credentials`, `<perimeter>-cookie` | `create` Secrets (namespace-wide) + `get` by `resourceNames` | create-if-missing credentials | Exception E1: `create` cannot be limited by `resourceNames` [V: comment in `credentials.tf`] |
| `publish-local-ca` (each trust namespace) | ConfigMaps `create/get/patch/update` (namespace-wide); `get` on the CA Secret by name | publishes `olf-local-ca` | Exception E1 (ConfigMaps only) |
| `polaris-bootstrap` (`olf-system` and each stage namespace) | ConfigMaps and Secrets `create/delete/get/patch/update`, namespace-wide [V: `rbac.tf`] | principal Secrets and their `-bootstrap-pending` markers | **Tighten (slice 4)**: every Secret and marker name is known at plan time (`*_credentials_secret_name`, `<name>-bootstrap-pending`, `polaris-dbt-creds`); `get/update/patch/delete` by `resourceNames`, `create` stays |
| `openmetadata-bootstrap` (`olf-system`, governed and revoked stage namespaces) | Secrets `create/delete/get/patch/update`, namespace-wide [V] | the ingestion-bot Secret | **Tighten**: by the bot Secret's name |
| OpenMetadata server and `openmetadata-ingestion` (chart `k8s-pipeline-rbac.yaml`) | pods, ConfigMaps, **Secrets** `get/list/create/update/patch/delete`, Jobs/CronJobs, `omjobs`, namespace-wide in `olf-system` [V] | runs ingestion pipelines as Jobs | **E2, the widest; slice 4 narrows it, it is not accepted by default.** A compromised OpenMetadata can read `keycloak-admin-creds`, the PostgreSQL admin and the Polaris root Secret. Limiting Secret verbs by `resourceNames` is not enough while the same Role can create pods, Jobs or CronJobs in `olf-system`: a workload mounts any Secret in its namespace and can run as any ServiceAccount there, including Traefik's and cert-manager's [D: Kubernetes RBAC good practices]. Slice 4 therefore counts workload creation as Secret access. It sets `pipelineServiceClientConfig.k8s.rbac.enabled: false` [V: key exists] and supplies a module-owned Role only if pod and Job creation can be confined (for example to a dedicated ingestion namespace or ServiceAccount); the server also creates Secrets with generated names [T]. Otherwise E2 is recorded as a named exception with this exact exposure, asserted by the pin check |
| `dagster`, `dagster-user-deployments` (stage) | Jobs and pods `create/delete/get/list/patch/update/watch`, pods `log/status`, events [V] | run launcher, Floe Jobs | Exception E3: no Secret verbs, but pod creation in a namespace reads that namespace's Secrets through volumes; a stage's user code already holds them. Stage namespace = trust domain |
| `traefik` ClusterRole | Secrets `get/list/watch` in **all** namespaces; Ingress status update [V] | TLS Secrets | Exception E4; narrowing to namespaces is a chart option, **T** |
| cert-manager ClusterRoles | Secrets `get/list/watch/create/update/delete` cluster-wide [V] | issuing certificates | Exception E5, inherent |
| `seaweedfs` ClusterRole `seaweedfs-rw-cr` | pods `get/list/watch/create/update/patch/delete` in **all** namespaces [V] | the chart's "hack for delete pod master after migration" | **Tighten**: `global.seaweedfs.createClusterRole: false` [V: values key]; that nothing needs it is **T** |
| `tigera-operator`, `calico-node` (Option B) | cluster-wide, privileged | the CNI | Exception E6, inherent |
| `olf` operator and CI | the kind kubeconfig is cluster-admin | everything | Trusted (#46). The non-admin recovery Role is in [`identity-sessions-and-recovery.md`](../architecture/identity-sessions-and-recovery.md): no Secret read, no `exec` |

Denied, and asserted with `kubectl auth can-i --as=system:serviceaccount:<ns>:<sa>`
(impersonation by the admin kubeconfig, no network involved): for the
`default` ServiceAccount of `olf-system` and of every stage namespace, `get`
and `list` Secrets, `create` pods, `create` rolebindings; for each Terraform
module ServiceAccount, `get` any Secret outside its `resourceNames`; for every
subject above, `create` ServiceAccount tokens, `escalate` or `bind` Roles, and
`patch` ConfigMap `coredns` in `kube-system` (the Corefile the
in-cluster-resolution module owns). A second assertion lists every Role and
ClusterRole bound to a ServiceAccount in the OLF namespaces that grants a
Secret verb, or create on pods, Jobs, CronJobs or workload controllers (which
reads Secrets by mounting them), and compares the set with E1 to E6: a new one fails the run until
this ADR names it.

## Conformance test

New `tools/olf/olf/e2e/_network.py`, called from `_runner.py` before the
pipeline checks, driven by `release/network-flows.yaml` (the flow rows, the
denied pairs, the RBAC expectations). Probe pods use the `k8s_bootstrap` image
(`curl`, `kubectl`, `python3`) the way `check_discovery_from_pod` does, with its
`sleep 3` attach-race note [V].

| Check | Setup | Expectation |
| --- | --- | --- |
| Canary | deny-all namespace pair, above | blocked, and the control pair connects |
| Negative, outside | a pod in a fresh namespace `olf-e2e-untrusted` with no OLF labels and no policies | TCP connect times out to Postgres 5432, Polaris 8181 and 8182, Trino 8080, Dagster 80 in each stage, Superset 8088, Keycloak 8080, SeaweedFS 8333 and 9333, OpenMetadata 8585 |
| Negative, inside | a pod in a stage namespace and one in `olf-system`, both without an allowing label | blocked towards Keycloak, Postgres (from `olf-system` pods outside F6), the other stage, SeaweedFS master and filer |
| Positive control | the same probe from the same namespace to Traefik 8443 (F1), and a pod carrying an allowed workload's labels (for example Superset's in a stage namespace) to Trino | connects, so a broken probe or DNS cannot pass the negatives vacuously |
| Back-channel | existing `check_discovery_from_pod` with its temporary policy (F26) | still returns the issuer |
| Authorized flows | the unchanged `olf e2e run --env local`: pipelines, Silver and Gold through Trino, dashboards, OpenMetadata assets, artifacts | pass with every policy on. This is the "unattended pipelines still work" evidence and needs no new code |
| kindnet semantics (Option A) | the canary's pairs extended with: an `ipBlock` allow and an `ipBlock` `except`; a kubelet probe and an API-server-to-webhook call into a default-deny pod (node-sourced traffic); a policy naming a pod port reached through a ClusterIP Service (DNAT order); a named port | each behaves as the flow table assumes; a failure here is a reason to build B |
| Port-forward | the existing forwards (SeaweedFS, Polaris, Keycloak, Dagster, Superset, OpenMetadata, portal) | pass; the run fails if any falls back to another path |
| RBAC | the can-i table and the Secret-verb pin above | as above |

Negative probes assert a timeout, not a refusal, and run with a 5 s budget;
the table's port list is generated from the flow file so a new denied pair
needs one line. The probe namespace and every temporary policy are removed in a
`finally`, as `mail_sink` does.

## Where the code goes

| What | Where |
| --- | --- |
| Flow rows, denied pairs, RBAC expectations | `release/network-flows.yaml`, schema in `docs/schema/`, validated by `olf check`; registered in `_structure.py` |
| Policies | New module `infra/terraform/modules/security/network-policy` (`main.tf`, `variables.tf`, `outputs.tf`; `kubernetes_network_policy_v1`), instantiated once in `environments/local/main.tf` after the namespaces and given the namespace names, the `kubernetes` Service's cluster IP and the API server endpoint (a `kubernetes_endpoints_v1` data source) for F20. Local root only; AWS and Azure are out of scope |
| RBAC changes | the modules that own the Roles (`catalog/polaris`, `governance/openmetadata`) and Helm values (`seaweedfs.yaml`) |
| CNI (Option B) | `infra/kind/local/`, `tools/olf/olf/deployment/local/foundation.py` plus a small `cni.py`, catalog entries, `prefetch.py` |
| Gate and canary | `foundation_up` / a deploy precondition, and `e2e/_network.py` |
| Docs flipped from "not yet verified" | [`identity-callers.md`](../architecture/identity-callers.md) Dagster and internal-reachability rows, ADR 0014 seam 3 paragraph, the #178 limitation line |

## Implementation plan

Each slice is one pull request of at most 600 changed lines, and the stack works
after every one.

| Slice | Content | Risk |
| --- | --- | --- |
| 1a | The canary and the deploy gate, run against the existing cluster. Settles A or B | Cheap. If kindnet blocks, 1b disappears |
| 1b (Option B only) | Kind config, catalog pins, `cni.py`, prefetch, install, readiness gate, forced-recreate message, tests. The footprint measured and recorded | Highest: VXLAN/BGP on WSL2, Docker Desktop and Colima **T**; Traefik `hostPort` through the CNI **T**; CI runner time |
| 2 | `release/network-flows.yaml` and its check, the module, and the **stage-namespace** policies (default-deny, DNS, intra-namespace, F5, F6, F7 to F12 egress, F20) | Dynamic pods with labels not read (Floe, run pods): the stage rule is namespace-wide on purpose |
| 3a | `olf-system` data plane: Postgres, SeaweedFS, Polaris, Trino ingress and egress | Postgres bootstrap Job pods carry the Postgres pod labels [V], so the Job is also "Postgres" to a selector; give it its own egress row |
| 3b | `olf-system` access plane: Traefik, oauth2-proxy, Keycloak, portal, cert-manager, OpenMetadata, OpenSearch, all Jobs | cert-manager's webhook is called by the API server from the node (F21): a wrong rule fails `terraform apply`. The F20 cluster-IP and endpoint, F22 and F23 rendered from the profile |
| 4 | RBAC: `resourceNames` for the Polaris and OpenMetadata bootstrap Roles, `createClusterRole: false`, `automount_service_account_token = false`, and the can-i and pin assertions | Removing a grant a chart quietly uses; the e2e is the check. E2 is the hard part |
| 5 | The conformance e2e matrix completed, `identity-callers.md` and ADR 0014 updated to "verified", #178's "unreachable from outside" box | The slices above each add their rows; this one adds the untrusted-namespace matrix and docs |

Ordering with the other v0.5 work: the Trino rows move to 8443 in the #26
cutover, and the OpenMetadata and Superset rows to Traefik (F4) when #25 lands,
each as an edit to the flow file in that PR.

## Not covered by #46

- Resource requests, limits, PodDisruptionBudgets and storage sizing: #351
  (v0.6). The Calico footprint above is measured, not budgeted.
- Pod Security Standards, non-root conversion and `securityContext`: #352
  (v0.8). Nothing here relies on a pod being unprivileged.
- AWS and Azure enforcement (VPC CNI / network policy engines, security
  groups), and public-domain qualification.
- Narrowing the Keycloak route, moving OpenMetadata to its own namespace,
  per-user data access (#179), and Trino authentication itself (#26).
- Egress filtering by hostname, mTLS between pods, and any service mesh.

## Decisions

1. **Calico is a fallback, not the plan.** The approval rested on the belief
   that kind does not enforce NetworkPolicy; the kind release notes say it
   does. Slice 1a (canary, with a positive control) decides; slice 1b is built
   only if the canary proves kindnet does not enforce. kube-network-policies is
   a reference implementation, so the conformance e2e tests the semantics the
   flow table relies on (`ipBlock`, node-sourced traffic, DNAT order).
2. **E2 and the over-broad Roles are narrowed, not accepted.** Slice 4 limits
   OpenMetadata's Secret and workload-creation access, the Polaris and OpenMetadata bootstrap Roles
   (`resourceNames`) and the SeaweedFS cluster role (`createClusterRole:
   false`); a grant that cannot be removed becomes a named, tested exception.
3. **The e2e's own probe pods carry a label the flow file names** (F26, F27)
   instead of getting temporary policies: both flows end or start at a
   default-deny workload (Traefik, Keycloak), so a policy removed in a `finally`
   would be the only thing standing between a crashed run and a broken stack.
   The probes stay in `olf-system`; the rows allow only the pod that sets the
   label, and creating pods there is already a cluster-administrator act.
4. **Stage-wide trust is accepted for local qualification.** Inside a stage
   namespace there are no per-pod rules, because user code launches pods with
   labels it controls. The limitation: anything running in a stage namespace
   can reach everything that namespace may reach.

## History

New record (#46 design): premise check on kindnet, the Calico install design
(fallback), the default-deny model with a derived flow table, the RBAC
inventory with its exceptions, the conformance plan, the slice order and the
maintainer's decisions. Nothing built.

Slice 1a built: `olf deploy --provider local` runs the canary
(`olf/deployment/local/network_canary.py`) after the foundation and fails
unless a deny-all namespace blocks a client its policy-free twin reaches.
(Superseded below by the one-pair, two-direction canary.)
Observed once, outside `olf`'s own cluster: on a stock kind v0.31.0 cluster
(not the pinned 0.32.0) with kindnetd
`v20251212-v0.29.0-alpha-105-g20ccfc88`, the gate passed, so kindnet blocked
the deny-all pair. That did not settle A on its own.

Option A settled (maintainer run, 2026-10-16): `olf deploy --provider local
--phase foundation` on the pinned kind v0.32.0, node `kindest/node:v1.36.1`,
kindnetd `v20260528-9350166c`, printed "NetworkPolicy is enforced." Slice 1b
(Calico) is dropped; the kindnet-semantics rows of the conformance test still
run in the policy slices.

Canary reworked after review: one server and client pair is proven to connect
with no policy, then an ingress-only and an egress-only deny must each block it.
The maintainer's run above used the earlier two-namespace canary. The rework
was then run against the same pinned cluster (kind v0.32.0, kindnetd
`v20260528-9350166c`) through `require_policy_enforcement` and printed
"NetworkPolicy is enforced.": kindnet blocks the pair for an ingress-only and an
egress-only deny. Option A stands.

Slice 2 built: `release/network-flows.yaml` holds the stage rows of the flow
table (F3 and F14 ingress, F5, F6, F9, F10, F12 and F20 egress), checked by
`olf check flows` (in `olf check all`) for scope, ports, tag and that every id
is a row of the table above. `modules/security/network-policy` renders, per
stage namespace, a default-deny policy for both directions and one allow
policy: the whole namespace may talk to itself, plus the file's rows.
Additions beyond the slice list, needed so the stack keeps working: F3
(Traefik into Dagster 80 and Superset 8088, namespace-wide) and F14 (from
`olf-system`, Superset 8088). F7, F8, F11 and F13 have `olf-system` sources and
land in slices 3a/3b. F10 allows both 8080 and 8443; the 8080 row is dropped
with the Trino HTTP listener. Still **[T]**: F9 (Floe runner and run pod
labels are not read, and the SeaweedFS `component=s3` label is from the chart,
not rendered here), F14, and F20 (whether the Service address, the endpoint
address or both reach the policy). The policy module was planned against a
stub provider, not applied.

Slice 2 runtime check (maintainer, 2026-10-16, kind v0.32.0, kindnetd
`v20260528-9350166c`): `olf deploy` and `olf e2e run --env local` pass with the
stage policies, so the pipeline flows in the file are sufficient. From a probe
pod in `olf-dev`, Trino (F10) answers and Keycloak (no flow) is refused by pod
IP, by ClusterIP and by name. **Observed limitation:** kindnet programs a new
pod's policy asynchronously, and a probe started seconds earlier still reached
Keycloak by pod IP and ClusterIP before the deny took effect. A freshly started
stage pod therefore has a short window of unrestricted egress. Run pods are
short-lived and start inside that window, so this is recorded as a known
exposure of the kindnet dataplane, to be measured and either accepted or
mitigated (for example an init-time wait) in slice 5's conformance matrix.

Slice 3a built: `workloads` in `release/network-flows.yaml` names the four
data-plane workloads by chart labels (`name` plus `instance`, and
`component=coordinator` for Trino), and the module renders one policy per
workload in `olf-system` denying both directions except the rows that name it.
Only those pods are selected; every other `olf-system` pod stays unisolated
until slice 3b, and there is no namespace-wide deny. Jobs share the `name`
label with their workload but not `instance`, so they are unselected and
their traffic is allowed at the destination by their `openlakeforge.io/job`
label (bucket, Polaris bootstrap and replication, Polaris metastore
bootstrap). The Postgres bootstrap Job is the exception the slice list
flagged: its pod carries the Postgres labels, so it is selected and has its own
egress row to Postgres (F6). Trino listens on 8080 and 8443 and both are
allowed. Callers of Trino from `olf-system` are one row from every pod of the
namespace (F11), **[T]**: OpenMetadata ingestion Job pod labels are set by the
server at runtime and are not read, so the row narrows to OpenMetadata and
Traefik in 3b once they are. Still **[T]**: F7 (Polaris to SeaweedFS), F10
Trino to itself (only needed if the CNI does not hairpin the Service address)
and the new-pod window recorded above, which now also covers these pods. The
module was validated and its rule sets evaluated with `terraform console`, not
applied.

Slice 3a runtime check (maintainer, kind, fresh cluster): `olf deploy` passes
and the allow and deny probes match. One e2e run failed at the Polaris restart
drill: the Trino query right after the new Polaris pod was Ready got an empty
response from Polaris, and two re-runs passed. Under kindnet a restarted,
policy-selected pod is briefly unreachable after Ready, the same asynchronous
programming seen for new egress above, now on a destination. The drill retries
its first post-restart query for up to 60 s instead of weakening the policies.
Slice 5 should measure the window.

Slice 3b built: `release/network-flows.yaml` gains workloads for Traefik, the
oauth2-proxy floors (a constant pod label `openlakeforge.io/workload` that the
perimeter module now sets, because the floors differ in name), the portal,
Keycloak, Dex, the three cert-manager pods and the `startupapicheck` Job,
OpenMetadata, OpenSearch and fourteen Jobs (selected by `openlakeforge.io/job`).
The module gives every workload DNS egress itself, adds the peer scopes `node`,
`apiserver` (as a source) and `any` (outside the pod and Service ranges, kind's
defaults), and lets one row list several sources. F1 is `any` to Traefik 8443
only; F21 allows the API server and node addresses to the webhook on 10250,
**[T]**: the source address kindnet shows for the API server's call is not read,
and a wrong rule fails `terraform apply`. F11 is narrowed from any `olf-system`
pod to Traefik, OpenMetadata and the OLF-owned ingestion Job; the Job label
`openmetadata-trino-ingestion` is the label #410 gives the Job, so
until that merges the OpenMetadata pipeline client's ingestion pods (labels not
read) cannot reach Trino. F14 is narrowed the same way. Dex gets F28, 443 on any
address, scoped to its pods. F22 (Keycloak to the profile's SMTP port) and F23
(oauth2-proxy to the issuer URL's port, 443 by default, when the issuer is `external`) are rendered from the
profile in `environments/local/main.tf`. Policies are rendered only for
deployed workloads: the local root drops Keycloak, Dex and the governance
workloads when the profile does not deploy them, and #406 must extend
`workload_deployed` for Traefik, cert-manager, the perimeter and the portal when
it makes them optional. Decision 3 is amended (above): F26 and F27 are rows
keyed on a label the e2e sets. Still **[T]**: OpenSearch's and OpenMetadata's
pod labels (the dependencies chart is not rendered here), OpenMetadata's own
Service hairpin, whether OpenMetadata calls Polaris or SeaweedFS (no row
exists; the Iceberg crawl was not traced), the Job labels of the OpenMetadata
pipeline client, and the new-pod window above, which now covers these pods
too. Validated with `terraform validate` and by evaluating the module's rule
sets with `terraform console`; not applied.
