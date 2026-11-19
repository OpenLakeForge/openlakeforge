# ADR 0007: the quality reconciler, one short-lived CronJob per governed stage.
# It replays a stage's quality manifests from the ops bucket into OpenMetadata
# test case results and never reruns a check. It runs the platform API image
# with another command, as the alert evaluator does, so no second image exists.
# Platform phase only (ADR 0002): it needs the stage's activation prefix and
# OpenMetadata, never project code. A stage without governance is absent from
# `stages`, so Slim renders no ServiceAccount, CronJob or credential.
locals {
  labels = { "app.kubernetes.io/name" = var.release_name }

  # The flow table selects the pods on this label (release/network-flows.yaml).
  job_label = "quality-reconciler"
}

# No Role is bound and the default token is not mounted: the reconciler holds
# no Kubernetes API access. Its credentials are the stage's ops-bucket key and
# the stage's quality-writer token, both from Secrets.
resource "kubernetes_service_account_v1" "reconciler" {
  count = length(var.stages) > 0 ? 1 : 0

  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  automount_service_account_token = false
}

resource "kubernetes_cron_job_v1" "reconciler" {
  for_each = var.stages

  metadata {
    name      = "${var.release_name}-${each.key}"
    namespace = var.namespace
    labels    = merge(local.labels, { "openlakeforge.io/stage" = each.key })
  }

  lifecycle {
    precondition {
      condition     = var.image != null
      error_message = "image is required when a stage has governance"
    }
  }

  spec {
    schedule = var.schedule
    # One pass per stage at a time: the state objects have a single writer, so a
    # run still going when the next is due makes that one skipped, not overlapped.
    concurrency_policy            = "Forbid"
    starting_deadline_seconds     = 120
    successful_jobs_history_limit = 1
    failed_jobs_history_limit     = 1

    job_template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"   = local.job_label
          "openlakeforge.io/stage" = each.key
        })
      }

      spec {
        # A failed pass is retried by the next schedule, from the state in the
        # bucket, not by the Job: the publisher already backs off per result.
        backoff_limit = 0
        # Under the schedule period, so a hung pass never holds the next off.
        active_deadline_seconds    = 240
        ttl_seconds_after_finished = 3600

        template {
          metadata {
            labels = merge(local.labels, {
              "openlakeforge.io/job"   = local.job_label
              "openlakeforge.io/stage" = each.key
            })
          }

          spec {
            restart_policy                  = "Never"
            service_account_name            = kubernetes_service_account_v1.reconciler[0].metadata[0].name
            automount_service_account_token = false

            container {
              name              = "reconciler"
              image             = var.image
              image_pull_policy = var.image_pull_policy
              command           = ["python", "-m", "openlakeforge_platform_api.quality_sync"]

              env {
                name  = "OPENLAKEFORGE_STAGE"
                value = each.key
              }
              env {
                name  = "OPENLAKEFORGE_ARTIFACT_BASE_URI"
                value = each.value.artifact_base_uri
              }
              env {
                name  = "OPENLAKEFORGE_CATALOG_DATABASE_FQN"
                value = each.value.database_fqn
              }
              env {
                name  = "OPENMETADATA_URL"
                value = var.openmetadata_url
              }
              env {
                name  = "OPENLAKEFORGE_S3_ENDPOINT_URL"
                value = var.s3_endpoint_url
              }
              env {
                name  = "OPENLAKEFORGE_S3_REGION"
                value = var.s3_region
              }
              env {
                name  = "OPENLAKEFORGE_QUALITY_SYNC_LIMIT"
                value = tostring(var.limit)
              }
              env {
                name  = "OPENLAKEFORGE_QUALITY_WARN_STATUS"
                value = var.warn_status
              }
              dynamic "env" {
                for_each = length(each.value.silver_schema_fqns) > 0 ? [1] : []
                content {
                  name  = "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON"
                  value = jsonencode(each.value.silver_schema_fqns)
                }
              }
              dynamic "env" {
                for_each = length(each.value.gold_schema_fqns) > 0 ? [1] : []
                content {
                  name  = "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON"
                  value = jsonencode(each.value.gold_schema_fqns)
                }
              }
              # Both credentials are references: neither value is in this
              # spec, the state or the pod's arguments.
              env {
                name = "AWS_ACCESS_KEY_ID"
                value_from {
                  secret_key_ref {
                    name = each.value.credentials_secret_name
                    key  = "AWS_ACCESS_KEY_ID"
                  }
                }
              }
              env {
                name = "AWS_SECRET_ACCESS_KEY"
                value_from {
                  secret_key_ref {
                    name = each.value.credentials_secret_name
                    key  = "AWS_SECRET_ACCESS_KEY"
                  }
                }
              }
              env {
                name = "OPENMETADATA_TOKEN"
                value_from {
                  secret_key_ref {
                    name = "${var.writer_secret_prefix}${each.key}"
                    key  = var.writer_jwt_key
                  }
                }
              }

              resources {
                requests = {
                  cpu    = "50m"
                  memory = "96Mi"
                }
                limits = {
                  memory = "256Mi"
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
          }
        }
      }
    }
  }
}
