resource "kubernetes_cron_job_v1" "catalog_refresh" {
  count = var.catalog_refresh_enabled ? 1 : 0

  metadata {
    name      = "openmetadata-polaris-refresh"
    namespace = var.namespace
    labels = merge(local.labels, {
      "openlakeforge.io/job" = "catalog-refresh"
    })
  }

  spec {
    schedule                      = var.catalog_refresh_schedule
    concurrency_policy            = "Forbid"
    successful_jobs_history_limit = 3
    failed_jobs_history_limit     = 3

    job_template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job" = "catalog-refresh"
        })
      }

      spec {
        backoff_limit = 1

        template {
          metadata {
            labels = merge(local.labels, {
              "openlakeforge.io/job" = "catalog-refresh"
            })
          }

          spec {
            restart_policy = "Never"

            container {
              name  = "catalog-refresh"
              image = var.bootstrap_job_image

              command = ["/bin/sh", "-ec"]
              args = [templatefile("${path.module}/templates/catalog-refresh.sh.tftpl", {
                om_url               = local.om_url
                admin_email          = var.admin_email
                admin_password       = var.admin_password
                catalog_service_name = local.catalog_service_name
              })]
            }
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_job_v1.bootstrap,
  ]
}
