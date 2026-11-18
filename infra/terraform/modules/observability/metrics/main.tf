# ADR 0024: one Prometheus server, pulling a minimal baseline and keeping it on
# its own volume. Platform phase only (ADR 0002). The root calls this module
# only when the resolved profile enables metrics, so Slim renders none of it.
locals {
  labels    = { "app.kubernetes.io/name" = var.release_name }
  http_port = 9090
  data_path = "/prometheus"
  token_dir = "/var/run/secrets/kubernetes.io/serviceaccount"

  # Decision 3: `stage` comes from the namespace (kubelet) or the bucket
  # (SeaweedFS), never from a value a workload sets.
  namespace_stages = merge({ for stage, namespace in var.stage_namespaces : namespace => stage }, { (var.namespace) = "platform" })
  stage_from = {
    for label, stages in { namespace = local.namespace_stages, bucket = var.bucket_stages } : label => [
      for value, stage in stages : { source_labels = [label], regex = value, target_label = "stage", replacement = stage }
    ]
  }

  # Decision 2's allowlists: everything else is dropped before it is stored.
  keep = {
    kubelet_resource = ["container_cpu_usage_seconds_total", "container_memory_working_set_bytes", "node_cpu_usage_seconds_total", "node_memory_working_set_bytes"]
    kubelet          = ["kubelet_volume_stats_capacity_bytes", "kubelet_volume_stats_used_bytes"]
    # The heap bean needs openmetrics.jmx-object-names on the coordinator, which slice M-c adds.
    trino        = ["trino_execution_name_QueryManager_RunningQueries", "trino_execution_name_QueryManager_QueuedQueries", "trino_execution_name_QueryManager_FailedQueries_TotalCount", "trino_execution_name_QueryManager_CompletedQueries_TotalCount"]
    object_store = ["SeaweedFS_volumeServer_resource", "SeaweedFS_volumeServer_total_disk_size", "SeaweedFS_s3_bucket_size_bytes", "SeaweedFS_s3_request_total"]
    # kube-state-metrics already exposes only these (--metric-allowlist); the
    # scrape keeps the same list, so a widened flag still stores nothing more.
    kube_state_metrics = local.ksm_metrics
    # `up` is written for every target by the scraper itself, so no allowlist can drop it.
    self = ["prometheus_tsdb_head_series", "prometheus_tsdb_storage_blocks_bytes", "prometheus_target_scrapes_exceeded_sample_limit_total"]
  }
  keep_rule = { for job, names in local.keep : job => { source_labels = ["__name__"], action = "keep", regex = join("|", names) } }

  # The kubelet takes the ServiceAccount token as a bearer and authorizes it
  # with a SubjectAccessReview for nodes/metrics.
  kubelet_job = {
    scheme                = "https"
    kubernetes_sd_configs = [{ role = "node" }]
    authorization         = { credentials_file = "${local.token_dir}/token" }
    tls_config            = { ca_file = "${local.token_dir}/ca.crt", insecure_skip_verify = var.kubelet_insecure_skip_verify }
    sample_limit          = 2000
    target_limit          = 20
    relabel_configs       = [{ source_labels = ["__meta_kubernetes_node_name"], target_label = "node" }]
  }

  config = {
    global = {
      scrape_interval          = "30s"
      scrape_timeout           = "10s"
      label_limit              = 16
      label_value_length_limit = 256
    }
    # The config-file form: at 3.13 the --storage.tsdb.retention.* flags are
    # deprecated for these fields. Whichever bound is reached first deletes
    # the oldest block, so a series explosion shortens history, not the disk.
    storage = {
      tsdb = {
        retention = { time = "${var.retention_days}d", size = var.retention_size }
      }
    }
    scrape_configs = [
      merge(local.kubelet_job, {
        job_name               = "kubelet-resource"
        metrics_path           = "/metrics/resource"
        metric_relabel_configs = concat([local.keep_rule.kubelet_resource], local.stage_from.namespace)
      }),
      merge(local.kubelet_job, {
        job_name               = "kubelet"
        metrics_path           = "/metrics"
        metric_relabel_configs = concat([local.keep_rule.kubelet], local.stage_from.namespace)
      }),
      {
        job_name       = "trino"
        static_configs = [for stage, target in var.trino_targets : { targets = [target], labels = { stage = stage, service = "trino" } }]
        # Trino has no authentication of its own (ADR 0016), but its
        # OpenMetrics endpoint still wants a user name.
        basic_auth             = { username = "olf-metrics" }
        sample_limit           = 500
        target_limit           = 3
        metric_relabel_configs = [local.keep_rule.trino]
      },
      {
        job_name               = "object-store"
        static_configs         = [for service, target in var.object_store_targets : { targets = [target], labels = { service = service } }]
        sample_limit           = 500
        target_limit           = 4
        metric_relabel_configs = concat([local.keep_rule.object_store], local.stage_from.bucket)
      },
      {
        job_name               = "kube-state-metrics"
        static_configs         = [{ targets = ["${local.ksm_name}.${var.namespace}.svc:${local.ksm_port}"] }]
        sample_limit           = 3000
        target_limit           = 1
        metric_relabel_configs = concat([local.keep_rule.kube_state_metrics], local.stage_from.namespace)
      },
      {
        job_name               = "prometheus"
        static_configs         = [{ targets = ["localhost:${local.http_port}"], labels = { stage = "platform" } }]
        sample_limit           = 200
        metric_relabel_configs = [local.keep_rule.self]
      },
    ]
  }
  config_yaml = yamlencode(local.config)
}

resource "kubernetes_service_account_v1" "prometheus" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  automount_service_account_token = false
}

# Node discovery and the kubelet's metrics endpoints, nothing else. Never
# nodes/proxy, which the kubelet treats as exec in any container; no pods,
# Services, endpoints or ConfigMaps, because every other target is static.
resource "kubernetes_cluster_role_v1" "prometheus" {
  metadata {
    name   = "${var.namespace}-${var.release_name}"
    labels = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["nodes"]
    verbs      = ["get", "list", "watch"]
  }
  rule {
    api_groups = [""]
    resources  = ["nodes/metrics"]
    verbs      = ["get"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "prometheus" {
  metadata {
    name   = kubernetes_cluster_role_v1.prometheus.metadata[0].name
    labels = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.prometheus.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.prometheus.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_config_map_v1" "prometheus" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  data = {
    "prometheus.yml" = local.config_yaml
  }
}

# A standalone claim, not a StatefulSet template, so a larger size expands it
# in place. Not waited on: under WaitForFirstConsumer it binds only once the
# pod below exists.
resource "kubernetes_persistent_volume_claim_v1" "data" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = var.volume_size
      }
    }
  }
  wait_until_bound = false
}

resource "kubernetes_deployment_v1" "prometheus" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    # Two replicas would write one TSDB twice; Recreate releases the volume first.
    replicas = 1
    strategy {
      type = "Recreate"
    }
    selector {
      match_labels = local.labels
    }

    template {
      metadata {
        labels = local.labels
        annotations = {
          "openlakeforge.io/config-hash" = sha256(local.config_yaml)
        }
      }

      spec {
        service_account_name            = kubernetes_service_account_v1.prometheus.metadata[0].name
        automount_service_account_token = false

        security_context {
          run_as_user     = 65534
          run_as_group    = 65534
          run_as_non_root = true
          fs_group        = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        # No admin API, no lifecycle endpoint, no remote write and no OTLP
        # receiver: nothing deletes series or reloads over HTTP.
        container {
          name              = "prometheus"
          image             = var.image
          image_pull_policy = "IfNotPresent"
          args = [
            "--config.file=/etc/prometheus/prometheus.yml",
            "--storage.tsdb.path=${local.data_path}",
            "--web.listen-address=:${local.http_port}",
            "--query.timeout=10s",
            "--query.max-samples=5000000",
            "--query.max-concurrency=4",
          ]

          port {
            name           = "http"
            container_port = local.http_port
          }

          volume_mount {
            name       = "config"
            mount_path = "/etc/prometheus"
            read_only  = true
          }
          volume_mount {
            name       = "data"
            mount_path = local.data_path
          }
          volume_mount {
            name       = "token"
            mount_path = local.token_dir
            read_only  = true
          }

          readiness_probe {
            http_get {
              path = "/-/ready"
              port = "http"
            }
            period_seconds = 10
          }
          liveness_probe {
            http_get {
              path = "/-/healthy"
              port = "http"
            }
            initial_delay_seconds = 30
            period_seconds        = 30
          }

          # ADR 0024's proposed starting values; slice M-f measures and replaces them.
          resources {
            requests = {
              cpu    = "100m"
              memory = "256Mi"
            }
            limits = {
              cpu    = "500m"
              memory = "768Mi"
            }
          }

          security_context {
            read_only_root_filesystem  = true
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }
        }

        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map_v1.prometheus.metadata[0].name
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.data.metadata[0].name
          }
        }
        # The token at the in-cluster path that node discovery and the kubelet
        # job read, at the shortest lifetime the API server issues: on kind the
        # kubelet's certificate is not checked, so a node impersonator could
        # replay it (ADR 0024, Decision 5).
        volume {
          name = "token"
          projected {
            sources {
              service_account_token {
                expiration_seconds = 600
                path               = "token"
              }
            }
            sources {
              config_map {
                name = "kube-root-ca.crt"
                items {
                  key  = "ca.crt"
                  path = "ca.crt"
                }
              }
            }
            sources {
              downward_api {
                items {
                  path = "namespace"
                  field_ref {
                    field_path = "metadata.namespace"
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}

# The platform API's one way in (F45); NetworkPolicy is the control, as for Trino.
resource "kubernetes_service_v1" "prometheus" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  spec {
    selector = local.labels
    port {
      name        = "http"
      port        = local.http_port
      target_port = "http"
    }
  }
}
