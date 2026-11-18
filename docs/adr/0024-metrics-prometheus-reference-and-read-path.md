# ADR 0024: Optional metrics: a bounded Prometheus reference and its read path

## Status

Proposed, awaiting the owner's approval (#21). Slices M-a, M-b and M-c under
"Build order" are built; the rest is not. This record decides the backend, the scrape
baseline, the label policy, the read path through the platform API, the network
and RBAC additions and the contract shape for `spec.observability.metrics`; the
slices under "Build order" implement it once it is approved.

Tags used below: **[V]** checked in this repository at `acba95b`, or in the
pinned upstream artifact or page named beside it, on 2026-11-17; **[T]** a claim
the implementing slice must test before anyone relies on it.

Every decision carries a basis. **Maintainer** means the owner fixed it in #21,
its 2026-11-16 comment or #183's 2026-11-03 decision, and it binds every slice.
**Default, revisit** means #21 or #332 recorded it as a default the owner has not
chosen explicitly. **Proposed** means this record had to fill a gap; each
proposal the owner should look at is a question under "Open items", with a
recommended answer.

## Context

[#21](https://github.com/OpenLakeForge/openlakeforge/issues/21) asks for an
optional, resource-bounded metrics backend whose history the native Portal
(#333) charts through the platform API (#793). Its 2026-11-16 comment binds the
scope: a **minimal** scrape baseline, reads only through #793's allowlisted
platform API, charts in the native Portal, no kube-prometheus-stack, and no
Grafana or Loki dependency. #183's 2026-11-03 decision makes metrics default on
for Full and off for Slim, both overridable, and lists as non-goals "in-browser
unrestricted PromQL", "per-run IDs as unbounded metrics labels" and a "full
tracing/OTel ingestion framework". The owner left one choice to this record:
whether OpenTelemetry has a part.

What the tree has today:

| Fact | Where it is verified |
| --- | --- |
| `spec.observability.metrics` takes `enabled` and `retention_days` (default 7, at most 3650); `enabled` follows the preset while unstated: `true` for Full, `false` for Slim | `_METRICS_FIELDS`, `DEFAULT_METRICS_RETENTION_DAYS`, `MAX_RETENTION_DAYS`, `_resolve_observability` in `tools/olf/olf/common/profile.py`; [deployment profile](../reference/deployment-profile.md#specobservability) **[V]** |
| The contract carries `shared.observability.metrics` as `{enabled, retention_days, retention_supported, supported}`; `supported` and `retention_supported` come from each root's `observability_support` | `outputs.tf` and `variables.tf` in `infra/terraform/modules/contracts/provider-contract/` **[V]** |
| All three roots report `metrics = false` and `metrics_retention = false`, so a Full profile reads as requested-but-unsupported; the local root's comment names #21 as the slice that flips them | `observability_support` in `infra/terraform/environments/{local,aws-poc,azure-poc}/contracts.tf` **[V]** |
| AWS lists `observability.cloudwatch` and `observability.managed_prometheus` as future adapter shapes, Azure `observability.azure_monitor` and `observability.managed_prometheus`; every root sets `tracing_enabled = false` | `observability_contract` in the same three files **[V]** |
| The contract validator rejects a field it does not know, so a new `metrics` field is a validator change | `_check_observability` in `tools/olf/olf/common/provider_contracts/_v3.py`; `_fields` in `_validation.py` **[V]** |
| The health routes already carry a `metrics` integration whose status is never a health state: `available`, `disabled`, `unavailable` or `not_provided`; today it is always `not_provided` | `IntegrationStatus` in `packages/platform-api/openlakeforge_platform_api/models.py`; `_METRICS_NOT_PROVIDED` in `health_routes.py` **[V]** |
| Every API route has one row in the permission matrix: a role floor, `stages`, optional `role_stages` and `services`; a route without a row is refused | `packages/platform-api/openlakeforge_platform_api/permissions.toml` **[V]** |
| The health routes' floor is `data-engineer` on every stage | `get_stage_health`, `get_stage_service_health` in `permissions.toml` **[V]** |
| `olf-system` and each stage namespace are default-deny both ways; every flow is a row in `release/network-flows.yaml`, whose last id is F44; a `node` peer renders as the nodes' `/32` addresses | `release/network-flows.yaml`; `node_cidrs` in `infra/terraform/modules/security/network-policy/`; [ADR 0017](0017-local-network-policy-and-rbac.md) **[V]** |
| The platform API holds no Kubernetes grant beyond a namespaced `list` of pods, Deployments and StatefulSets and a TokenReview `create` | `modules/access/platform-api/probe_rbac.tf`; [ADR 0021](0021-platform-api.md), decision 7 **[V]** |
| Pins: Trino image `480` and chart `1.42.2`; Dagster chart and image `1.13.7`; SeaweedFS chart `4.48.0` and image `4.48`. No Prometheus, kube-state-metrics or collector image is pinned | `release/component-catalog.yaml` **[V]** |
| kind is pinned at `0.32.0` and the foundation sets no node image, so nodes run kind's default, `kindest/node:v1.36.1` | `components.toolchain.kind` in the catalog; `infra/terraform/foundations/local-kind/main.tf`; `pkg/apis/config/defaults/image.go` at kind `v0.32.0` **[V]** |
| Full with DEV and PROD runs about thirty pods at steady state | [diagrams](../architecture/diagrams.md), chart 1 **[V]** |
| `spec.storage.size` picks one row of `release/storage-sizes.yaml` (`small`, `medium`, `large`), read by Terraform and `olf` | `release/storage-sizes.yaml` **[V]** |
| Stage buckets are bound per stage; the ops bucket is shared | `stage_storage`, `bucket_names` in `infra/terraform/environments/local/main.tf` **[V]** |
| No workload sets `openlakeforge.io/domain` or `openlakeforge.io/product` | [ADR 0022](0022-pod-log-collection-and-archive-format.md), decision 4 **[V]** |
| Trino has no authentication of its own | `modules/query/trino/ui.tf`; [ADR 0016](0016-trino-workload-authentication.md) **[V]** |
| Nothing in the platform emits OTLP: no OTLP port (4317, 4318) or `OTEL_` setting in `infra/`, `images/`, `release/` or `packages/`; `opentelemetry-api` 1.45.2 is locked only as a dependency of FastAPI (and of dbt-common in the tooling lock), with no SDK or exporter in any lock | grep of the tree; `images/platform-api/requirements.lock` ("via fastapi"); `tools/olf/uv.lock`; `images/project-code/requirements.lock` has none **[V]** |

## Decision 1: one Prometheus server, no collector

| Basis | Decision |
| --- | --- |
| Maintainer | The backend is a resource-bounded Prometheus reference, not kube-prometheus-stack, with no Grafana or Loki dependency. |
| Proposed | **One Prometheus server** in `olf-system`, a single replica, pulling every target itself and storing locally. No OpenTelemetry Collector and no agent mode in v0.7. |
| Proposed | Prometheus **3.13**, the long-term-support line (released 2026-07-01, supported until 2027-07-31), at **3.13.5**, not the newest 3.16.0. |
| Proposed | Deployed by a Terraform module, `infra/terraform/modules/observability/metrics`, from `kubernetes_*` resources and pinned images, as the log collector is; not the community chart. |
| Maintainer | It exists only when the resolved `metrics.enabled` is true and the provider reports `metrics.supported`. Otherwise the module creates nothing: no pod, PVC, Service, ConfigMap, Role or flow row. |

The three shapes weighed, against pins and pages read on 2026-11-17:

| | Prometheus server | OpenTelemetry Collector into Prometheus | Prometheus agent mode |
| --- | --- | --- | --- |
| Components | One | Two: the collector scrapes, Prometheus still stores and answers queries | One that cannot answer a query: agent mode "disables some of Prometheus' usual features (TSDB, alerting, and rule evaluations)" ([agent mode](https://prometheus.io/docs/prometheus/latest/prometheus_agent/)) **[V]**, so a second, queryable store is needed |
| Image (Docker Hub, compressed, amd64) | `prom/prometheus:v3.13.5`, 106,844,245 bytes **[V]** | adds `otel/opentelemetry-collector-k8s:0.165.0`, 51,228,635 bytes, or `-contrib` 114,867,530 **[V]** | as the server, plus the store |
| Gains | Pull, local TSDB, PromQL, recording rules, scrape limits | OTLP and trace ingestion, processors | Remote write to an external store |
| Useful when | Every source exposes a Prometheus or OpenMetrics endpoint, which is the case for every source in Decision 2 | An OTLP or trace source exists. None does (Context, last row), and #183 makes tracing a non-goal | The store is external (Decision 6) |

The recommendation is the server alone. The collector adds a second image, pin,
NetworkPolicy and failure mode for a gain no source can use today. If an OTLP
metrics source arrives, Prometheus itself has an OTLP receiver (the `otlp`
section of its [configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
**[V]**; the flag that enables it at 3.13 is **[T]**), so a single source does
not force a collector. A collector is reconsidered when traces are in scope or
several OTLP sources need routing. Open item 1 puts this to the owner.

Why the LTS line: an LTS release "receive[s] selected bug fixes for an extended
period of one year" ([long-term support](https://prometheus.io/docs/introduction/release-cycle/)
**[V]**), while a minor release is superseded every six weeks. 3.13.5 was
published on 2026-10-21 beside 3.15.1, and 3.16.0 on 2026-11-05 (Docker Hub
**[V]**). The slice records the index digest in the catalog, as ADR 0022 did
for Fluent Bit; the one read on 2026-11-17 is
`sha256:aafa9d694e6a883c02db516d9f6e5b84e14195f49a2a6456a4cca815edda72b9`.

Why not the community `prometheus` chart (29.47.2, app `v3.16.0` **[V]**): it
bundles Alertmanager, kube-state-metrics, node-exporter and Pushgateway as
subcharts, adds a config-reload sidecar image, and its ClusterRole reads
`configmaps`, `services`, `endpoints`, `pods` and `ingresses` in every namespace
(`templates/clusterrole.yaml` of 29.47.2 **[V]**). Turning those off and
replacing the RBAC leaves a Deployment, a PVC and a ConfigMap, which the
existing modules already write directly. Open item 9.

### Shape and limits

| Setting | Value | Why |
| --- | --- | --- |
| Workload | Deployment, one replica, `strategy: Recreate`, and a separate PersistentVolumeClaim | A standalone claim can be expanded in place; a StatefulSet's claim template cannot change, which is why `olf` deletes the SeaweedFS StatefulSets to grow them ([deployment profile](../reference/deployment-profile.md#changing-the-storage-size)). Two replicas would write one TSDB twice |
| Scrape interval | 30 s, timeout 10 s | The Portal's finest step is 60 s (Decision 4) |
| Retention | `storage.tsdb.retention.time` (`<retention_days>d`) and `storage.tsdb.retention.size` from the size row below, in the configuration file; whichever is reached first deletes the oldest block. The `--storage.tsdb.retention.*` flags are deprecated at 3.13.5 in favour of these fields (`docs/command-line/prometheus.md` at `v3.13.5` **[V]**) | Without either setting the default is 15 d ([storage](https://prometheus.io/docs/prometheus/latest/storage/) **[V]**); the size cap is the loss point an operator can reason about |
| Query limits | `--query.timeout=10s`, `--query.max-samples=5000000`, `--query.max-concurrency=4` | The API sends a few small range queries per chart; these stop a bad template from exhausting the pod **[T: flag names at 3.13]** |
| Admin | No `--web.enable-admin-api`, no `--web.enable-lifecycle`, no remote write, no OTLP receiver | Nothing deletes series or reloads over HTTP; a configuration change is a rollout |
| Pod | uid 65534, read-only root filesystem except the data volume, no capability, `RuntimeDefault` seccomp | As the platform API **[T: the image's default user]** |
| Resources | Proposed requests 100m CPU and 256Mi; limits 500m and 768Mi | Starting values; the slice measures and replaces them (Build order) |

Storage sizes. Prometheus "stores an average of only 1-2 bytes per sample", and
keeps at least three 128 MB write-ahead-log segments ([storage](https://prometheus.io/docs/prometheus/latest/storage/)
**[V]**). Sizing uses a **series budget of 10,000** active series. The budget
is an operating target the e2e measures, **not a ceiling Prometheus enforces**:
`sample_limit` bounds one scrape, not the head series that accumulate as pods
come and go, and the limits below permit far more than 10,000 at their maxima
(20 kubelet targets at 2,000 samples alone allow 40,000). What does bound the
disk is `retention.size`: whatever the series count, the oldest blocks go first,
so a series explosion shortens history instead of filling the volume. 10,000
series every 30 s is 333 samples per second, at 2 bytes about 58 MB a day, so
7 days about 0.4 GB and 30 days about 1.7 GB, plus the write-ahead log. The baseline expected on
Full with two stages is a few thousand series **[T: the slice measures it]**,
well under the budget.

| `spec.storage.size` | Metrics volume | `retention.size` | Fits at the full budget |
| --- | --- | --- | --- |
| `small` | 4Gi | 3GB | about 50 days |
| `medium` | 8Gi | 6GB | about 100 days |
| `large` | 16Gi | 12GB | about 200 days |

The rows go in `release/storage-sizes.yaml` as a `metrics` key beside
`object_store` (Proposed), so one file still decides every self-hosted volume.
`retention_days` keeps its single default of 7 for every size and preset
(Default, revisit: #332's value). The volume scales with size because medium
and large run Trino workers and more pods, not because they need longer history.
Compaction can briefly exceed `retention.size` ([storage](https://prometheus.io/docs/prometheus/latest/storage/)
**[V]**), which is why the cap is 75% of the volume. Open item 2.

## Decision 2: the minimal scrape baseline

| Basis | Decision |
| --- | --- |
| Maintainer | A minimal, meaningful baseline, not every available exporter; what is unsupported is recorded. |
| Proposed | Five sources, each with a `keep` allowlist of metric names applied at scrape time, so everything else is dropped before it is stored. |

| Source | What exposes it at the pinned version | How it is scraped | Kept (allowlist) |
| --- | --- | --- | --- |
| kubelet resource metrics | The kubelet serves `/metrics/resource` and `/metrics/cadvisor` besides `/metrics` ([system metrics](https://kubernetes.io/docs/concepts/cluster-administration/system-metrics/) **[V]**). `/metrics/*` is authorized as `nodes/metrics`; **`nodes/proxy` is not read-only** ("authorizes executing commands in any container") ([kubelet authn/authz](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/) **[V]**) | HTTPS to each node's port 10250, discovered with `kubernetes_sd_configs` role `node`, the ServiceAccount token as bearer. `/metrics/resource`, not `/metrics/cadvisor`: the resource endpoint carries the CPU and working-set series the charts need and far fewer series **[T: series count measured]** | `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `node_cpu_usage_seconds_total`, `node_memory_working_set_bytes` **[T: names at 1.36]** |
| kubelet volume stats | The kubelet's own `/metrics` | Same target, path `/metrics` | `kubelet_volume_stats_capacity_bytes`, `kubelet_volume_stats_used_bytes`. Whether kind's local-path volumes report them is **[T]**; if they do not, PVC usage is unsupported on local |
| kube-state-metrics | Chart `9.0.1`, app `2.20.0` (prometheus-community index **[V]**); image `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0`, pinned in the catalog by its index digest. The seven names below are in `docs/metrics/workload/*.md` at `v2.20.0` **[V]** | A Deployment in `olf-system` limited by `--namespaces` to the OLF namespaces and by `--resources` to `pods`, `deployments`, `statefulsets` and `daemonsets`, with `--metric-allowlist` and `--metric-labels-allowlist=pods=[app.kubernetes.io/name,app.kubernetes.io/component]`. The chart notes that without a ClusterRole it runs "ONLY ... if namespace is also set" (`values.yaml` of 9.0.1 **[V]**), so its grants are Roles; the module deploys it without the chart | `kube_pod_container_status_restarts_total`, `kube_pod_status_ready`, `kube_pod_labels`, `kube_pod_created`, `kube_deployment_status_replicas_available`, `kube_statefulset_status_replicas_ready`, `kube_daemonset_status_number_ready` |
| Trino | Trino 480 serves OpenMetrics at `/metrics` on the coordinator, "automatically enabled", "protected with the configured authentication"; a client names a user with `X-Trino-User` or a basic-auth user and no password; `openmetrics.jmx-object-names` selects further JMX beans ([Trino 480 OpenMetrics](https://trino.io/docs/480/admin/openmetrics.html) **[V]**) | Each stage's coordinator Service, port 8080, as static targets Terraform renders from the stage map (`service_name`, `http_port` in `modules/query/trino/outputs.tf` **[V]**; that the release name is the coordinator's Service name is **[T]**), with basic-auth user `olf-metrics` and no password. The slice adds `openmetrics.jmx-object-names` through `additionalConfigProperties` (`modules/query/trino/config.tf` **[V]**) for the memory bean. Workers are not scraped: `small` has none | `trino_execution_name_QueryManager_RunningQueries` (named by the page's example **[V]**), `..._QueuedQueries`, `..._FailedQueries`, `..._CompletedQueries`, and `JMX_java_lang_TYPE_Memory_ATTRIBUTE_HeapMemoryUsage_used`. A `CounterStat` is one counter named after its attribute, with no `_TotalCount`, and the property adds beans to the default series rather than replacing them (`MetricsResource`, `Counter` and `CompositeMetric` of airlift 419, the version Trino 480 builds with; `DispatchManager` and `QueryManagerStats` at Trino `480` **[V]**) |
| SeaweedFS | Chart 4.48.0 sets `metricsPort: 9327` on master, volume, filer and S3 and renders `-metricsPort` whenever it is set; `global.seaweedfs.monitoring.enabled` (default `false`) only adds the container port declaration, ServiceMonitors and a Grafana dashboard ConfigMap (`values.yaml`, `templates/*/*-statefulset.yaml`, `*-servicemonitor.yaml` of the pinned chart, whose sha256 matches the catalog **[V]**). The process serves `/metrics` on that port (`StartMetricsServer`, `weed/stats/metrics.go` at tag 4.48 **[V]**); that it listens without the declaration is **[T]** | The four Services' `metrics` port 9327, static targets. `monitoring.enabled` stays `false`: its ServiceMonitors need the operator's CRDs | `SeaweedFS_volumeServer_resource{name,type}` (disk use), `SeaweedFS_volumeServer_total_disk_size`, `SeaweedFS_s3_bucket_size_bytes{bucket}`, `SeaweedFS_s3_request_total{type,code,bucket}` (`metrics.go` at 4.48 **[V]**) |
| Prometheus itself | Its own `/metrics` | `localhost:9090` | `up` of every job, `prometheus_tsdb_head_series`, `prometheus_tsdb_storage_blocks_bytes`, `prometheus_target_scrapes_exceeded_sample_limit_total` |

Two upstream behaviours the charts must respect. SeaweedFS deletes a bucket's
size gauges after ten minutes in which it recorded no activity for that bucket
(`bucketMetricTTLControl`, `bucketAtiveTTL` in `metrics.go` at 4.48 **[V]**);
whether a size refresh counts as activity is **[T]**, and until it is known a
gap in the storage trend reads as `unavailable`, never as zero. Trino counts per
node, so a later `medium` or `large` coordinator reports only its own share
**[V: "Each Trino node ... provide[s] separate metrics independently"]**.

### Dagster is not scraped

Dagster 1.13.7 has no Prometheus endpoint: its chart has no metrics values
outside the bundled Redis subchart, and the `dagster`, `dagster-webserver` and
`dagster-k8s` wheels at the pinned versions contain no Prometheus client
**[V: `helm` chart 1.13.7; grep of the three wheels]**. `dagster-prometheus`
(0.29.30 on PyPI) is a resource user code calls to push to a Pushgateway; it
requires `dagster==1.13.30` and is not in `images/project-code/requirements.lock`
**[V]**.

| Basis | Decision |
| --- | --- |
| Proposed | Execution counters and durations are **not Prometheus series**. The platform API derives them from the Dagster runs it already reads (`DagsterRun.created_at`, `started_at`, `ended_at`, `status` in `openlakeforge_services/dagster.py` **[V]**) and serves them through the same series envelope as Decision 4, with `source: dagster`. |

Why: Dagster's run storage is already the record of every run, retained
beyond any Prometheus window; an exporter polling it would be a second copy
that can disagree with the runs route, one more Dagster reader and flow, and a
source of per-job label sets; a Pushgateway would need user code to push and
would keep the last value of a job that stopped. Derived in the API, the
runs and durations chart also works on Slim, where no Prometheus runs. The read
is bounded: a range of at most 7 days and at most 500 runs per stage, the newest
first **[T: the GraphQL filter and limit the slice uses]**. A cap can cut a
range short, so the API never serves a capped read as complete history: when
the cap is reached, the response carries `complete: false` and `covered_from`,
the oldest run's creation time, and every step before `covered_from` is `null`.
A busy pipeline can still displace a quieter one inside the covered interval;
per-job reads, each capped, are the slice's alternative if the measured stage
volume makes that common. Open item 5.

### Unsupported, recorded

| Not collected | Why |
| --- | --- |
| node-exporter (host filesystem, network, load) | A host-network DaemonSet for numbers the charts do not need; node CPU and memory come from the kubelet |
| `/metrics/cadvisor` | Same CPU and memory with many more series per container |
| API server, scheduler, CoreDNS, etcd | Cluster internals; no Portal chart reads them |
| Dagster, Superset, Polaris, OpenMetadata, PostgreSQL, Traefik | No endpoint at the pinned version (Dagster), or not in #333's chart list. Their health stays the service probes ([ADR 0021](0021-platform-api.md), decision 7) |
| Trino workers | `small` has none; added with the `medium` and `large` Trino sizes (#324) |
| Fluent Bit's own metrics | Its delivery signals are the spool probe ([ADR 0022](0022-pod-log-collection-and-archive-format.md), decision 2); the engine's output metrics are "meaningless for S3" |
| Per-pod disk on kind | Depends on the kubelet volume stats row above **[T]** |
| Anything on AWS or Azure | Decision 6 |

## Decision 3: labels and cardinality

| Basis | Decision |
| --- | --- |
| Maintainer | The labels a served series may carry are `stage`, `component`, `service` and `domain`. No run ID is a label. |
| Proposed | The API serves only **recording-rule series** named `olf:<name>`, whose labels are a subset of those four. Raw scraped series are inputs to the rules and are never served. |
| Proposed | Scrape-time relabelling sets `stage` from the namespace (each stage namespace to its stage, `olf-system` to `platform`) and from the bucket for SeaweedFS (each stage's buckets to its stage, the ops bucket to `platform`), from maps Terraform renders, as the log collector's routes are rendered. |

| Label | Value | Bound |
| --- | --- | --- |
| `stage` | `dev`, `uat`, `prod`, `platform` | Four |
| `component` | the pod's `app.kubernetes.io/name`, joined from `kube_pod_labels`; `unlabelled` when absent | The workloads of the census, about twenty |
| `service` | the object-store process (`master`, `volume`, `filer`, `s3`) or `trino` | Five |
| `domain` | `openlakeforge.io/domain` | Reserved: no workload sets it yet (Context), so no series carries it until the pipeline slices of ADR 0022 add the label |

Run IDs do reach raw series, and the record says so rather than hiding it: the
pinned `dagster-k8s` 0.29.7 names a run's Job `dagster-run-<run_id>`
(`dagster_k8s/job.py` **[V]**), so the kubelet's and kube-state-metrics' `pod`
label carries a run ID for every run pod. Those raw series live for the run's
lifetime plus the staleness window and are bounded by how many pods run at
once, not by how many runs exist; they are kept for the retention window on
disk, which the size cap bounds. Dropping the `pod` label at scrape time is not
possible without merging different pods' samples into one series. The
alternative is to drop run pods' series entirely, which loses the pipelines'
share of stage CPU and memory. Open item 4.

Limits, so a misbehaving target fails visibly instead of growing the database:

| Limit | Value | Effect |
| --- | --- | --- |
| `sample_limit` per scrape | kubelet 2,000 per node; kube-state-metrics 3,000; Trino 500 per coordinator; SeaweedFS 500 per process; self 200 | "If more than this number of samples are present after metric relabeling the entire scrape will be treated as failed" ([configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/) **[V]**); `up` turns 0 and the API answers `unavailable` |
| `label_limit` | 16 | Same failure mode **[V]** |
| `label_value_length_limit` | 256 bytes | Same failure mode **[V]** |
| `target_limit` per job | kubelet 20; Trino 3; SeaweedFS 4; kube-state-metrics 1 | Experimental upstream **[V]**; a target explosion fails the job |
| Recording rule groups | each group sets `limit`, the series a rule may produce, at 500 | A rule over the limit fails its evaluation and records nothing (`docs/configuration/recording_rules.md` at `v3.13.5` **[V]**). The limit is per rule, so it also bounds the `kube_pod_labels` join at 500 pods |
| Series budget | `prometheus_tsdb_head_series` under 10,000 on the reference deployment | An operating target, not an enforced bound: the e2e asserts it on one deployment, and a deployment that exceeds it loses history to `retention.size` rather than disk. Head series above the budget for 30 minutes is a `warning` on the metrics integration, so the overrun is visible **[T: the slice wires the signal]** |

## Decision 4: the read path

| Basis | Decision |
| --- | --- |
| Maintainer | Only the platform API queries Prometheus. It serves a fixed allowlist of named series with bounded ranges and steps; no route accepts PromQL. |
| Maintainer | Prometheus unavailable, a target down or a series absent reads as `unknown`/`unavailable`, never `healthy`, and never as zero. |
| Proposed | The catalog of named series lives in `openlakeforge_core` (one module, read by the API and by tests); each entry names its scope, unit, the recording rule it reads and the labels it returns. The query text is server-side and parameterised only by values the API validated. |
| Proposed | Metrics never feed `HealthState`. A chart is evidence beside the health signals, not one of them. |

Routes, all read-only and not audited, like the health routes:

| Route | Returns |
| --- | --- |
| `GET /api/v1/stages/{stage}/metrics` | The catalog for that stage: each series' name, unit, labels and its availability (`available`, `disabled`, `unavailable`, `not_provided`, the existing `IntegrationStatus` vocabulary) |
| `GET /api/v1/stages/{stage}/metrics/{series}?range=<r>` | One series' points for the stage |
| `GET /api/v1/platform/metrics/{series}?range=<r>` | A `platform`-scope series: nodes and `olf-system` components |

| Parameter | Allowed | Step | Points |
| --- | --- | --- | --- |
| `range=1h` | 1 hour ending now | 60 s | 60 |
| `range=6h` | 6 hours | 60 s | 360 |
| `range=24h` | 24 hours | 5 min | 288 |
| `range=7d` | 7 days, refused when `retention_days` is shorter | 30 min | 336 |

No `start`, `end`, `step` or query parameter exists. A range longer than the
retention is a 400, not a short answer that looks complete. The API's client
times out after 5 s, as the service probes do (`PROBE_TIMEOUT` in
`service_probes.py` **[V]**).

The first catalog, mapped to #333's chart list:

| Series | Scope | Labels | Source | #333 chart |
| --- | --- | --- | --- | --- |
| `cpu_usage_cores` | stage, platform | `stage`, `component` | kubelet, joined to `kube_pod_labels` | service CPU |
| `memory_working_set_bytes` | stage, platform | `stage`, `component` | kubelet, joined | service memory |
| `container_restarts` | stage, platform | `stage`, `component` | kube-state-metrics: the rule records each minute's restarts per container before summing, from range selectors so a pod deleted within the minute still counts. A container first seen that minute counts its whole value only if `kube_pod_created` puts its pod in the last two minutes; otherwise, after a scrape gap or lost history, it counts 0, so a gap undercounts and never replays a lifetime. The API sums it per step | (health context) |
| `node_cpu_usage_cores`, `node_memory_working_set_bytes` | platform | none | kubelet | cluster CPU and memory |
| `trino_running_queries`, `trino_queued_queries` | stage | `stage` | Trino | Trino load |
| `trino_failed_queries` | stage | `stage` | Trino, increase per step | Trino query errors |
| `trino_heap_used_bytes` | stage | `stage` | Trino `java.lang:type=Memory` **[T]** | Trino memory |
| `object_store_bucket_bytes` | stage, platform | `stage` | SeaweedFS S3, summed by stage | storage trend |
| `object_store_disk_used_bytes`, `object_store_disk_free_bytes` | platform | `service` | SeaweedFS volume server | storage trend |
| `pipeline_runs`, `pipeline_run_duration_seconds` | stage | `stage` and the job, as pipeline health is | Dagster reads, not Prometheus (Decision 2) | jobs duration and failures |

Response envelope, for both sources:

```json
{"series": "trino_failed_queries", "stage": "dev", "source": "prometheus",
 "state": "available", "range": "6h", "step_seconds": 60, "unit": "queries",
 "observed_at": "2026-11-17T10:00:00Z",
 "lines": [{"labels": {"stage": "dev"},
            "points": [["2026-11-17T04:01:00Z", 0], ["2026-11-17T04:02:00Z", null]]}]}
```

A step without a sample is `null`, never `0`. A Dagster-sourced series adds
`complete` and, when `false`, `covered_from` (Decision 2); a Prometheus series
is always `complete: true` within retention. `state` is `unavailable` with no
`lines` when Prometheus does not answer or a target behind the series has
`up == 0` for the whole range; `disabled` when the profile turns metrics off;
`not_provided` when the provider reports `metrics.supported = false`. The stage
health route's `metrics` integration takes the same value instead of today's
fixed `not_provided`.

### Authorization rows

In the style of `permissions.toml`, the floor of the health routes, because a
resource chart shows less than the health and runs routes already do:

```toml
# Resource history from the metrics backend (ADR 0024). The API sends only the
# named series of the catalog; no route takes a query. Read-only, not audited.
[operations.list_stage_metrics]
role = "data-engineer"
stages = ["*"]

[operations.get_stage_metric]
role = "data-engineer"
stages = ["*"]

# Nodes and olf-system components serve every stage, so the row names none.
[operations.get_platform_metric]
role = "data-engineer"
stages = []
```

No `role_stages`: a series carries no log text or data (Proposed; Open item 7).
No `services`: the alert evaluator does not read metrics, since #23's alerts
work without Prometheus ([ADR 0023](0023-alert-evaluation-and-webhook-delivery.md)).

## Decision 5: network and RBAC

| Basis | Decision |
| --- | --- |
| Proposed | Prometheus and kube-state-metrics run in `olf-system`. Prometheus scrapes the stage namespaces only for the Trino coordinators; stage pods' CPU and memory come from the kubelet on the node, not from the pods. |
| Proposed | Prometheus has no authentication of its own in v0.7. Its one ingress row is the platform API; NetworkPolicy is the control, as for Trino (ADR 0016). Open item 6. |

New workloads in the flow file: `prometheus: {app.kubernetes.io/name: prometheus}`
and `kube-state-metrics: {app.kubernetes.io/name: kube-state-metrics}`. Rows,
each rendered only when the module is deployed:

| # | From | To | Port | Note |
| --- | --- | --- | --- | --- |
| F45 | the platform API | Prometheus (`olf-system`) | 9090 | `query_range` and `query` for the catalog's series only, the API's own setting `metrics_url` |
| F46 | Prometheus | kube-apiserver | 443, 6443 | node discovery (`kubernetes_sd_configs` role `node`) |
| F47 | Prometheus | `node` | 10250 | kubelet `/metrics/resource` and `/metrics`, with its ServiceAccount token. Whether the enforcing CNI applies an egress `ipBlock` to a node address as written is **[T]**, as F21 is for the reverse direction |
| F48 | Prometheus | stage namespaces (each stage's Trino coordinator) | 8080 | `/metrics`; the stage rule is namespace-wide, as F11 and F36 |
| F49 | Prometheus | SeaweedFS (master, volume, filer, S3) | 9327 | `/metrics`; F17 does not list 9327 today **[V]** |
| F50 | Prometheus | kube-state-metrics | 8080 | `/metrics`; the image's entrypoint sets `--port=8080` (image config of `v2.20.0` **[V]**) |
| F51 | kube-state-metrics | kube-apiserver | 443, 6443 | list and watch in the OLF namespaces |

Denied by construction: every other ingress to Prometheus (no Portal, browser,
Traefik route or stage pod reaches it), and every egress beyond these rows.

RBAC inventory additions for ADR 0017:

| Subject | Grant | Used for | Verdict |
| --- | --- | --- | --- |
| `prometheus` (`olf-system`; ClusterRole `olf-system-prometheus` and its binding) | `nodes` `get`, `list`, `watch`; `nodes/metrics` `get`. Nothing else: no `nodes/proxy`, no pods, Services, endpoints or ConfigMaps (its other targets are static) | node discovery and the kubelet scrape | **Done if built as written**: `nodes/metrics` is the only kubelet subresource; `nodes/proxy`, which the kubelet treats as exec ([kubelet authn/authz](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/) **[V]**), is never granted. It exposes node objects and per-container usage, no Secret, log or spec. Matches no E1 to E6 rule |
| `kube-state-metrics` (`olf-system`; a Role and RoleBinding in each OLF namespace) | `pods`, `deployments`, `statefulsets`, `daemonsets` `list`, `watch` | the four collectors of Decision 2 | **Done if built as written**: read-only and namespaced. It reads full pod specs, including environment values, in those namespaces, which is wider than the platform API's `list` but the same data. It needs no `customresourcedefinitions` grant: the chart adds that rule only with `customResourceState.enabled` (`templates/role.yaml` of 9.0.1 **[V]**), and the module configures no custom-resource state |

Server identity on the kubelet scrape. The bearer token authenticates
Prometheus, and the kubelet's `SubjectAccessReview` authorizes it
**[T: kind's kubelet runs `--authorization-mode=Webhook`]**. Prometheus cannot
verify the kubelet's certificate on kind, whose kubelets serve self-signed
certificates unless the foundation enables `serverTLSBootstrap` and approves the
CSRs **[T]**. Skipping verification is **not only a data-integrity risk**: an
endpoint that impersonates a node address receives Prometheus's ServiceAccount
bearer token, and can replay it with all of Prometheus's grants, `nodes`
`get`/`list`/`watch` as well as `nodes/metrics`, until the projected token
expires. That exposes node objects and per-container usage, no Secret, log, exec
or spec; it is still a credential disclosure. The proposal for the **local kind
reference only** is `insecure_skip_verify` on that one job, with the projected
token's lifetime at the minimum (`expirationSeconds: 600`) and the risk recorded
as a limitation; any provider other than local must verify kubelet
certificates, and enabling `serverTLSBootstrap` in the kind foundation removes
the exception. Open item 8.

## Decision 6: provider neutrality

| Basis | Decision |
| --- | --- |
| Maintainer | The profile names a capability, never a product: no field selects Prometheus. |
| Proposed | `shared.observability.metrics` gains two optional fields, `query_url` and `query_api`; local fills them when the module is deployed. |
| Proposed | AWS and Azure keep `metrics = false` and `metrics_retention = false`: a Full profile there reads `enabled: true, supported: false`, and the Portal shows `not_provided`. |

```json
"metrics": {"enabled": true, "retention_days": 7, "retention_supported": true,
            "supported": true, "query_api": "prometheus-http-v1",
            "query_url": "http://prometheus.olf-system.svc:9090"}
```

`query_api` names the protocol the API speaks, not the product:
`prometheus-http-v1` is the Prometheus HTTP API's `GET /api/v1/query_range` and
`/api/v1/query` with PromQL. `query_url` is `null` whenever `supported` is
false. `retention_supported` turns true on local because the TSDB itself
deletes blocks past `retention.time`, unlike the archive's lifecycle rule
(ADR 0022, decision 6). The validator learns the two fields as optional, so
contracts already in state stay readable.

An external Prometheus-compatible backend (Thanos, Mimir, VictoriaMetrics, a
managed Prometheus) satisfies the contract when it:

1. answers `prometheus-http-v1` at a URL the API reaches through a flow row;
2. holds the catalog's `olf:` recording rules over the same baseline, with the
   label rules of Decision 3; the platform ships the scrape configuration and
   rule file the reference uses, so an operator can load them;
3. needs no authentication beyond what the API can send. v0.7's client sends
   none; a bearer token from a Secret is the natural next step. AWS managed
   Prometheus signs requests with SigV4 and Azure's managed Prometheus takes an
   Entra token **[T: both, from their pages, not read for this record]**, which
   is why both stay unsupported here.

Agent mode remote-writing to such a store is the shape an external adapter would
use for collection. Neither the adapter nor a profile field to select it is part
of #21; adding one is a contract change with its own issue.

## Alternatives considered

| Option | Why not |
| --- | --- |
| kube-prometheus-stack (93.3.1 **[V]**) | Maintainer: not this. The operator, its CRDs, Alertmanager, Grafana and node-exporter are far past a minimal baseline |
| OpenTelemetry Collector in front of Prometheus | Decision 1: a second component and nothing to ingest; Prometheus's own OTLP receiver covers one future source |
| Prometheus agent mode alone | Cannot answer queries; needs a store (Decision 1) |
| The community `prometheus` chart with subcharts off | Decision 1: a wide ClusterRole to replace and a sidecar image to pin, for a Deployment, a PVC and a ConfigMap |
| Raw PromQL through the API, filtered | #183 non-goal; a filter on a query language is a parser the API would have to keep correct |
| Browser or Portal to Prometheus directly | #333: the Portal reads only the platform API; Prometheus has no per-stage authorization |
| A Dagster exporter or Pushgateway for run metrics | Decision 2: a second copy of the runs, and user code would have to push |
| node-exporter | Decision 2: not needed for the charts |
| `/metrics/cadvisor` | Decision 2: same numbers, more series |
| The kubelet through the API server proxy (`nodes/proxy`) | Exec-equivalent grant ([kubelet authn/authz](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/) **[V]**) |
| Two Prometheus replicas | Twice the footprint for history that is a convenience; an outage reads as `unavailable` (Decision 4) and costs no ETL |
| A Prometheus per stage | Three TSDBs for a few thousand series; stage isolation is the API's rows, as for logs |

## Consequences

- Full gains two pods (Prometheus, kube-state-metrics) and one PVC in
  `olf-system`; Slim, or Full with `metrics.enabled: false`, gains nothing, and
  `metrics_url` is unset so the API answers `disabled`.
- An outage of Prometheus touches no ETL path: no workload sends to it, and the
  Portal shows `unavailable` charts while health, runs, logs and quality keep
  working.
- Stage isolation is the permission matrix, not the store. One Prometheus holds
  every stage, so any future consumer that sends its own PromQL (Grafana, #210)
  needs its own answer to stage authorization; #210 owns that, and adds its own
  flow row to 9090.
- The platform API gains no Kubernetes permission; the kubelet read is
  Prometheus's, scoped to `nodes/metrics`.
- Run IDs exist in raw series' `pod` label for the retention window (Decision 3)
  unless the owner chooses to drop run pods' series.
- History is lost with the PVC; nothing backs it up. It is a convenience, not
  evidence, and the evidence (runs, logs, quality) lives in Dagster and the ops
  bucket.
- AWS and Azure keep advertising metrics as unsupported until a provider adapter
  qualifies.

## Build order

Each slice stays under the 15-file, 600-line cap in AGENTS.md. M-a lands the
contract with no adapter; M-b and M-c are the local adapter; M-d and M-e are the
read path; M-f is the acceptance.

| Slice | Content | Updates |
| --- | --- | --- |
| M-a | `query_url` and `query_api` as optional contract fields; the validator; the local root renders `null` until M-b | `_v3.py` and its tests; `docs/architecture/provider-contracts.md` |
| M-b | `modules/observability/metrics`: the Prometheus Deployment, PVC, ConfigMap (scrape jobs for kubelet, Trino, SeaweedFS and itself, the limits of Decision 3), the ClusterRole, the `metrics` rows of `release/storage-sizes.yaml`, the catalog pin, flows F45 to F49, `metrics = true` and `metrics_retention = true` on local, Terraform tests that a disabled profile renders no resource | ADR 0017 flow table and RBAC inventory; `release/network-flows.yaml`; `release/component-catalog.yaml` |
| M-c | kube-state-metrics (pin, Roles, F50 and F51), the `kube_pod_labels` join and the `olf:` recording rules with their group limits; the Trino `openmetrics.jmx-object-names` property | ADR 0017; `modules/query/trino/config.tf` |
| M-d | The series catalog in `openlakeforge_core`, a Prometheus client in `openlakeforge_services`, the three routes and their matrix rows, the `metrics` integration status, the committed OpenAPI document; tests for every state including Prometheus down, a target down and a range past retention | ADR 0021; `docs/schema/` |
| M-e | `pipeline_runs` and `pipeline_run_duration_seconds` from Dagster reads, on Slim and Full | `pipeline_health.py` or a sibling |
| M-f | The e2e on a Full kind deployment: one series of each source through the API; Slim with metrics off has no Prometheus or kube-state-metrics pod, PVC, Service or flow; Prometheus scaled to zero reads `unavailable`, never healthy, while a pipeline still runs; `prometheus_tsdb_head_series` under the budget and no `olf:` series with a label outside Decision 3. **Measured footprint per size** (CPU, memory, PVC use after 24 hours, series count) recorded in the reference, replacing the proposed requests and limits | `docs/reference/deployment-profile.md`; a metrics reference page; `docs/technical-debt.md` if a limitation stays |

The Portal's charts are [openlakeforge-portal#6](https://github.com/OpenLakeForge/openlakeforge-portal/issues/6),
in the Portal repository, against M-d's OpenAPI document.

## Open items

Questions for the owner. Each names this record's recommended answer; the
decisions above assume it.

| # | Question | Recommended answer |
| --- | --- | --- |
| 1 | Does OpenTelemetry have a part in v0.7? | **No.** One Prometheus server. Revisit when traces are in scope or several OTLP sources exist; for one OTLP metrics source, enable Prometheus's own OTLP receiver first |
| 2 | Retention and volume defaults | **7 days for every size and preset** (the existing default); volumes of 4, 8 and 16 Gi for `small`, `medium`, `large`, with `retention.size` at 75% of each |
| 3 | Is kube-state-metrics part of the baseline? | **Yes, restricted** to the OLF namespaces, four resources and an allowlist: it is the only source of restart history and of the pod-to-component labels that make CPU and memory chartable per component. Without it, restarts are not charted and CPU and memory are charted per stage only |
| 4 | Run IDs in the raw `pod` label of run pods | **Accept** them in raw series that are never served, bounded by concurrent pods and the size cap. The alternative, dropping run pods' series, loses the pipelines' share of stage usage |
| 5 | Execution counters and durations | **From Dagster reads in the API**, not Prometheus; they then work on Slim too |
| 6 | Prometheus authentication | **None in v0.7**: one NetworkPolicy ingress row from the platform API. Basic auth or TLS via `--web.config.file` is added when a second consumer (#210) needs a route |
| 7 | Read floor | **`data-engineer` on every stage**, the health routes' floor, for stage and platform series alike |
| 8 | Kubelet certificate verification on kind | **Skip verification on the kubelet job, on local only**, accepting that an impersonated node address receives Prometheus's token (node reads and usage, no Secrets) for at most its 10-minute lifetime; every other provider verifies; enabling `serverTLSBootstrap` in the kind foundation is a separate change that removes the exception |
| 9 | Chart or Terraform resources | **Terraform `kubernetes_*` resources** with pinned images, as the log collector and the alert evaluator are |
| 10 | Version line | **Prometheus 3.13 LTS** (3.13.5), moving to the next LTS (due 2027-06) rather than following every minor |

Not verified for this record, each **[T]** above: the exact metric names at the
pinned Kubernetes version beyond those quoted;
whether SeaweedFS listens on 9327 without the container port declaration; kind's
kubelet authorization mode and certificates; volume stats on kind's local-path
volumes; the Prometheus 3.13 flag names for query limits and the OTLP receiver;
the managed Prometheus authentication schemes on AWS and Azure; and every
footprint figure.

## History

2026-11-17: created for #21, as a draft awaiting the owner's approval. Nothing
built.

2026-11-18: M-a and M-b built. Retention moved from the `--storage.tsdb.retention.*`
flags, deprecated at 3.13.5, to the same two fields in the configuration file.

2026-11-18: M-c built. kube-state-metrics v2.20.0 runs on a Role per OLF namespace,
and Prometheus loads the `olf:` recording rules, one group per source, each with
`limit: 500`. Trino's counters are named without `_TotalCount`. Its heap series is
`JMX_java_lang_TYPE_Memory_ATTRIBUTE_HeapMemoryUsage_used`, and the property adds to
the default series. `container_restarts` records restarts per minute, because a
counter summed across pods reads as a reset when a pod goes.
