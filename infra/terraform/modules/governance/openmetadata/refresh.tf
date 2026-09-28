resource "kubernetes_cron_job_v1" "catalog_refresh" {
  count = var.catalog_refresh_enabled && local.catalog_type == "rest" ? 1 : 0

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
                om_url                        = local.om_url
                admin_email                   = var.admin_email
                admin_password                = var.admin_password
                token_uri                     = var.catalog_contract.token_uri
                oauth_scope                   = var.catalog_contract.oauth_scope
                catalog_warehouse             = var.catalog_contract.warehouse
                catalog_database_name         = local.catalog_database_name
                catalog_rest_uri              = var.catalog_contract.rest_uri
                storage_region                = var.storage_contract.region
                storage_endpoint              = var.storage_contract.virtual_host_endpoint
                catalog_schema_names_json_b64 = local.catalog_schema_names_json_b64
              })]

              env {
                name = "POLARIS_OM_CLIENT_ID"
                value_from {
                  secret_key_ref {
                    name = var.catalog_contract.om_credentials_secret_name
                    key  = var.catalog_contract.om_client_id_key
                  }
                }
              }

              env {
                name = "POLARIS_OM_CLIENT_SECRET"
                value_from {
                  secret_key_ref {
                    name = var.catalog_contract.om_credentials_secret_name
                    key  = var.catalog_contract.om_client_secret_key
                  }
                }
              }

              env {
                name = "AWS_ACCESS_KEY_ID"
                value_from {
                  secret_key_ref {
                    name = var.storage_contract.credentials_secret_name
                    key  = var.storage_contract.access_key_id_key
                  }
                }
              }

              env {
                name = "AWS_SECRET_ACCESS_KEY"
                value_from {
                  secret_key_ref {
                    name = var.storage_contract.credentials_secret_name
                    key  = var.storage_contract.secret_access_key_key
                  }
                }
              }
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
