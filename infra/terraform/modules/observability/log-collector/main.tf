# ADR 0022: a node-level Fluent Bit that archives pod logs continuously, so a
# pod deleted between the polling CronJob's runs still reaches the archive.
# Platform phase only (ADR 0002): it reads the node's log files and the pod
# metadata the API server holds, and never project code.
locals {
  labels    = { "app.kubernetes.io/name" = var.release_name }
  http_port = 2020

  config = templatefile("${path.module}/templates/fluent-bit.conf.tftpl", {
    routes           = var.routes
    bucket           = var.bucket
    region           = var.s3_region
    endpoint         = var.s3_endpoint_url
    http_port        = local.http_port
    mem_buf_limit    = var.mem_buf_limit
    max_chunks_up    = var.max_chunks_up
    chunk_limit_size = var.chunk_limit_size
    spool_limit_size = var.spool_limit_size
  })

  # Builds the archive record (ADR 0022, decision 4) from the file name, the
  # CRI timestamp and the labels the kubernetes filter adds. It writes a new
  # table, so nothing a workload prints can add or replace a field. A label
  # that is absent is named in `missing` instead of being invented.
  record_lua = <<-LUA
    function build(tag, ts, record)
      local pod, namespace, container, container_id = (record.filepath or ""):match("([^/_]+)_([^/_]+)_([^/_]+)%-(%x+)%.log$")
      local labels = (record.kubernetes or {}).labels or {}
      local time = os.date("!%Y-%m-%dT%H:%M:%S", ts.sec) .. string.format(".%09dZ", ts.nsec)

      local run_id = labels["dagster/run-id"]
      if run_id == nil and pod then
        run_id = pod:match("^dagster%-run%-(%x+%-%x+%-%x+%-%x+%-%x+)")
      end

      local out = {
        schema = "openlakeforge.k8s_log.v2",
        time = time,
        date = os.date("!%Y-%m-%d", ts.sec),
        stage = record.stage,
        namespace = namespace,
        pod = pod,
        container = container,
        container_id = container_id,
        stream = record.stream,
        component = labels["app.kubernetes.io/name"],
        domain = labels["openlakeforge.io/domain"],
        product = labels["openlakeforge.io/product"],
        run_id = run_id,
        message = record.log,
        dedup_key = string.format("%s:%s:%d", container_id or "", time, record.offset or 0),
      }
      local missing = {}
      for _, field in ipairs({ "component", "domain", "product", "run_id" }) do
        if out[field] == nil then
          missing[#missing + 1] = field
        end
      end
      out.missing = missing
      return 2, ts, out
    end
  LUA
}

resource "kubernetes_service_account_v1" "collector" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
}

# The kubernetes filter's documented minimum: pods and namespaces, read-only.
# Cluster-wide, because the collector enriches records of every collected
# namespace; no nodes/proxy (Use_Kubelet is off), no Secrets, no write verb.
resource "kubernetes_cluster_role_v1" "collector" {
  metadata {
    name   = "${var.namespace}-${var.release_name}"
    labels = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["pods", "namespaces"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "collector" {
  metadata {
    name   = kubernetes_cluster_role_v1.collector.metadata[0].name
    labels = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.collector.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.collector.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_config_map_v1" "collector" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "fluent-bit.conf" = local.config
    "record.lua"      = local.record_lua
  }
}

resource "kubernetes_daemon_set_v1" "collector" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    selector {
      match_labels = local.labels
    }

    template {
      metadata {
        labels = local.labels
        annotations = {
          "openlakeforge.io/config-hash" = sha256("${local.config}${local.record_lua}")
        }
      }

      spec {
        service_account_name = kubernetes_service_account_v1.collector.metadata[0].name

        container {
          name              = "fluent-bit"
          image             = var.image
          image_pull_policy = "IfNotPresent"
          command           = ["/fluent-bit/bin/fluent-bit"]
          args              = ["-c", "/fluent-bit/etc/fluent-bit.conf"]

          port {
            name           = "http"
            container_port = local.http_port
          }

          env {
            name = "AWS_ACCESS_KEY_ID"
            value_from {
              secret_key_ref {
                name = var.credentials_secret_name
                key  = "AWS_ACCESS_KEY_ID"
              }
            }
          }
          env {
            name = "AWS_SECRET_ACCESS_KEY"
            value_from {
              secret_key_ref {
                name = var.credentials_secret_name
                key  = "AWS_SECRET_ACCESS_KEY"
              }
            }
          }

          volume_mount {
            name       = "config"
            mount_path = "/fluent-bit/etc"
            read_only  = true
          }
          volume_mount {
            name       = "varlog"
            mount_path = "/var/log"
            read_only  = true
          }
          volume_mount {
            name       = "state"
            mount_path = "/var/fluent-bit"
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }

          readiness_probe {
            http_get {
              path = "/api/v1/health"
              port = "http"
            }
            period_seconds = 10
          }

          resources {
            requests = {
              cpu    = "25m"
              memory = "64Mi"
            }
            limits = {
              memory = var.memory_limit
            }
          }

          # Root reads the kubelet's root-owned 0640 log files; nothing else
          # about the container is privileged.
          security_context {
            run_as_user                = 0
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
            name = kubernetes_config_map_v1.collector.metadata[0].name
          }
        }
        volume {
          name = "varlog"
          host_path {
            path = "/var/log"
          }
        }
        volume {
          name = "state"
          host_path {
            path = var.host_state_path
            type = "DirectoryOrCreate"
          }
        }
        volume {
          name = "tmp"
          empty_dir {
            size_limit = "64Mi"
          }
        }
      }
    }
  }
}
