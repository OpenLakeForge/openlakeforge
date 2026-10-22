locals {
  port   = 8080
  labels = { "app.kubernetes.io/name" = var.release_name }

  # The image reads this on every request (openlakeforge-portal README). Admin
  # is a link, not a contract route: with GitHub, members and roles are teams.
  routes = merge(var.links, var.account_url == null ? {} : {
    "shared/admin" = { stage = "shared", name = "admin", url = var.account_url }
  })
}

resource "kubernetes_config_map_v1" "routes" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "routes.json" = jsonencode(local.routes)
  }
}

moved {
  from = kubernetes_config_map_v1.site
  to   = kubernetes_config_map_v1.routes
}

resource "kubernetes_deployment_v1" "portal" {
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
      }

      spec {
        container {
          name  = "portal"
          image = var.image

          port {
            name           = "http"
            container_port = local.port
          }

          volume_mount {
            name       = "routes"
            mount_path = "/config"
            read_only  = true
          }

          readiness_probe {
            http_get {
              path = "/healthz"
              port = "http"
            }
            period_seconds = 10
          }

          resources {
            requests = {
              cpu    = "5m"
              memory = "48Mi"
            }
            limits = {
              memory = "128Mi"
            }
          }

          security_context {
            run_as_user                = 65534
            run_as_non_root            = true
            read_only_root_filesystem  = true
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }
        }

        volume {
          name = "routes"
          config_map {
            name = kubernetes_config_map_v1.routes.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "portal" {
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
