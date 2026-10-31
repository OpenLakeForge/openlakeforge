locals {
  port   = 8080
  labels = { "app.kubernetes.io/name" = var.release_name }

  # The API reads this once at startup (packages/platform-api/README.md).
  settings = {
    issuer_url   = var.issuer_url
    audiences    = var.audiences
    role_claim   = var.role_claim
    role_mapping = var.role_mapping
    precedence   = var.precedence
    stages       = var.stages
    ca_file      = var.ca_configmap_name == null ? null : "/ca/ca.crt"
    # Read endpoints (ADR 0021, build step 3); a service left out is answered
    # as not configured rather than refusing the API's start.
    dagster_graphql_urls = var.dagster_graphql_urls
    openmetadata_url     = var.openmetadata_url
    # Read with the key in log_reader_secret_name, which never enters this file.
    log_archives    = var.log_archives
    s3_endpoint_url = var.s3_endpoint_url
    s3_region       = var.s3_region
    # Written with the key in audit_writer_secret_name; without it, every
    # operation that changes state answers 503.
    audit_base_uri = var.audit_base_uri
  }
}

resource "kubernetes_config_map_v1" "settings" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "settings.json" = jsonencode(local.settings)
  }
}

resource "kubernetes_deployment_v1" "platform_api" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    replicas = 1

    selector {
      match_labels = local.labels
    }

    template {
      metadata {
        labels = local.labels
        # Settings are read at startup only, so a change must roll the pod.
        annotations = { "openlakeforge.io/settings-sha256" = sha256(jsonencode(local.settings)) }
      }

      spec {
        # ADR 0021 decision 6: no Kubernetes permission beyond its own mounts.
        automount_service_account_token = false

        container {
          name              = "platform-api"
          image             = var.image
          image_pull_policy = var.image_pull_policy

          port {
            name           = "http"
            container_port = local.port
          }

          # AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, which boto3 reads: the
          # read-only key for the log prefixes the SeaweedFS credentials Job mints.
          dynamic "env_from" {
            for_each = var.log_reader_secret_name == null ? [] : [1]
            content {
              secret_ref {
                name = var.log_reader_secret_name
              }
            }
          }

          # The write-only audit key, under names boto3 does not read on its
          # own, so it can never stand in for the log reader's key.
          dynamic "env" {
            for_each = var.audit_writer_secret_name == null ? {} : {
              OPENLAKEFORGE_AUDIT_ACCESS_KEY_ID     = "AWS_ACCESS_KEY_ID"
              OPENLAKEFORGE_AUDIT_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
            }
            content {
              name = env.key
              value_from {
                secret_key_ref {
                  name = var.audit_writer_secret_name
                  key  = env.value
                }
              }
            }
          }

          volume_mount {
            name       = "settings"
            mount_path = "/config"
            read_only  = true
          }

          dynamic "volume_mount" {
            for_each = var.ca_configmap_name == null ? [] : [1]
            content {
              name       = "ca"
              mount_path = "/ca"
              read_only  = true
            }
          }

          # /api/healthz never calls the issuer, so an issuer outage does not
          # restart or unready the pod.
          readiness_probe {
            http_get {
              path = "/api/healthz"
              port = "http"
            }
            period_seconds = 10
          }

          liveness_probe {
            http_get {
              path = "/api/healthz"
              port = "http"
            }
            initial_delay_seconds = 10
            period_seconds        = 20
          }

          resources {
            requests = {
              cpu    = "10m"
              memory = "64Mi"
            }
            limits = {
              memory = "192Mi"
            }
          }

          security_context {
            run_as_user                = 65534
            run_as_group               = 65534
            run_as_non_root            = true
            read_only_root_filesystem  = true
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
            seccomp_profile {
              type = "RuntimeDefault"
            }
          }
        }

        volume {
          name = "settings"
          config_map {
            name = kubernetes_config_map_v1.settings.metadata[0].name
          }
        }

        dynamic "volume" {
          for_each = var.ca_configmap_name == null ? [] : [1]
          content {
            name = "ca"
            config_map {
              name = var.ca_configmap_name
            }
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "platform_api" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    selector = local.labels

    port {
      name        = "http"
      port        = local.port
      target_port = "http"
    }
  }
}
