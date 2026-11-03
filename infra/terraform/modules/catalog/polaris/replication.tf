locals {
  # Every principal credential a stage-scoped workload mounts. The Trino and
  # OpenMetadata principals are consumed by shared services, but copying them
  # too keeps one rule rather than a per-consumer exception list.
  replicated_secret_names = [
    var.trino_credentials_secret_name,
    var.floe_credentials_secret_name,
    var.om_credentials_secret_name,
    var.deployer_credentials_secret_name,
  ]
}

resource "kubernetes_job_v1" "credential_replication" {
  count = var.replicate_credentials && length(var.workload_namespaces) > 0 ? 1 : 0

  metadata {
    name      = "polaris-credential-replication-${local.workload_revision}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "catalog-credential-replication"
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
            secret_names = join(" ", local.replicated_secret_names)
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
