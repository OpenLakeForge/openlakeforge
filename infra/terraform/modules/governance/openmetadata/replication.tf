resource "kubernetes_job_v1" "credential_replication" {
  count = length(var.workload_namespaces) + length(var.revoked_namespaces) > 0 ? 1 : 0

  metadata {
    name      = "openmetadata-credential-replication-${local.workload_revision}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "governance-credential-replication"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.bootstrap.metadata[0].name

        container {
          name  = "replicate"
          image = var.bootstrap_job_image

          command = ["/bin/sh", "-ec"]
          args = [templatefile("${path.module}/templates/replicate-secrets.sh.tftpl", {
            secret_names = var.ingestion_bot_secret_name
          })]

          env {
            name = "NAMESPACE"
            value_from {
              field_ref {
                field_path = "metadata.namespace"
              }
            }
          }

          env {
            name  = "WORKLOAD_NAMESPACES"
            value = join(" ", var.workload_namespaces)
          }

          env {
            name  = "REVOKED_NAMESPACES"
            value = join(" ", var.revoked_namespaces)
          }

          resources {
            requests = {
              cpu    = "10m"
              memory = "32Mi"
            }
            limits = {
              memory = "128Mi"
            }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
  }

  depends_on = [
    kubernetes_job_v1.bootstrap,
    kubernetes_role_binding_v1.bootstrap_workload,
  ]
}
