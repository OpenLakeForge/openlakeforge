# Runs `openlakeforge_runtime.k8s_log_archive` from the project-code image, so it belongs
# wherever user code does (ADR 0002). When activation owns user code it is
# rendered into the openlakeforge-project release instead, which also keeps it
# on the digest that stage actually activated.
resource "kubernetes_cron_job_v1" "kubernetes_log_archive" {
  count = var.manage_user_deployments ? 1 : 0

  metadata {
    name      = "openlakeforge-k8s-log-archive"
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/name"       = "openlakeforge-k8s-log-archive"
      "app.kubernetes.io/managed-by" = "terraform"
      "openlakeforge.io/component"   = "observability"
    }
  }

  spec {
    schedule                      = var.kubernetes_log_archive_schedule
    concurrency_policy            = "Forbid"
    successful_jobs_history_limit = 1
    failed_jobs_history_limit     = 3

    job_template {
      metadata {
        labels = {
          "app.kubernetes.io/name"     = "openlakeforge-k8s-log-archive"
          "openlakeforge.io/component" = "observability"
        }
      }

      spec {
        backoff_limit = 1

        template {
          metadata {
            labels = {
              "app.kubernetes.io/name"     = "openlakeforge-k8s-log-archive"
              "openlakeforge.io/component" = "observability"
            }
          }

          spec {
            service_account_name            = "dagster"
            automount_service_account_token = true
            restart_policy                  = "Never"

            container {
              name              = "archive-k8s-logs"
              image             = "${var.project_code_image_repository}:${var.project_code_image_tag}"
              image_pull_policy = var.project_code_image_pull_policy
              command           = ["python", "-m", "openlakeforge_runtime.k8s_log_archive"]

              dynamic "env" {
                for_each = local.log_archive_env
                content {
                  name  = env.value.name
                  value = env.value.value
                }
              }

              dynamic "env_from" {
                for_each = var.storage_contract.credentials_secret_name == null ? [] : [var.storage_contract.credentials_secret_name]
                content {
                  secret_ref {
                    name = env_from.value
                  }
                }
              }

              resources {
                requests = {
                  cpu    = "50m"
                  memory = "128Mi"
                }
                limits = {
                  memory = "256Mi"
                }
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    helm_release.dagster,
  ]
}
