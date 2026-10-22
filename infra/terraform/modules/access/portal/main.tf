locals {
  port   = 8080
  labels = { "app.kubernetes.io/name" = var.release_name }
  groups = {
    for stage in distinct([for link in values(var.links) : link.stage]) : stage => [
      for link in values(var.links) : link if link.stage == stage
    ]
  }

  # Rendered from the contract's routes, never listed by hand. The values are
  # hostnames and service names Terraform derived itself, so nothing is escaped.
  index_html = <<-EOT
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenLakeForge</title></head>
    <body>
    <h1>OpenLakeForge</h1>
    %{for stage in sort(keys(local.groups))~}
    <h2>${stage}</h2>
    <ul>
    %{for link in local.groups[stage]~}
    <li><a href="${link.url}">${link.name}</a></li>
    %{endfor~}
    </ul>
    %{endfor~}
    </body>
    </html>
  EOT

  # Served outside the perimeter (ADR 0014 Logout): the perimeter cookie is
  # gone, but the identity provider's own session is not touched.
  signed_out_html = <<-EOT
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Signed out</title></head>
    <body>
    <h1>You are signed out of OpenLakeForge</h1>
    <p>Your GitHub session is still active. Sign out of GitHub too on a shared browser.</p>
    <p><a href="https://github.com/logout">Sign out of GitHub</a> | <a href="/">Sign in again</a></p>
    </body>
    </html>
  EOT
}

resource "kubernetes_config_map_v1" "site" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "index.html"      = local.index_html
    "signed-out.html" = local.signed_out_html
  }
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
        # A changed page rolls the pod; projected ConfigMaps refresh lazily.
        annotations = {
          "openlakeforge.io/site-checksum" = sha256("${local.index_html}${local.signed_out_html}")
        }
      }

      spec {
        container {
          name  = "httpd"
          image = var.image
          # busybox's built-in server: no config, well under 1 MiB of heap.
          command = ["httpd", "-f", "-p", tostring(local.port), "-h", "/www"]

          port {
            name           = "http"
            container_port = local.port
          }

          volume_mount {
            name       = "site"
            mount_path = "/www"
            read_only  = true
          }

          readiness_probe {
            tcp_socket {
              port = local.port
            }
            period_seconds = 10
          }

          resources {
            requests = {
              cpu    = "5m"
              memory = "8Mi"
            }
            limits = {
              memory = "32Mi"
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
          name = "site"
          config_map {
            name = kubernetes_config_map_v1.site.metadata[0].name
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
