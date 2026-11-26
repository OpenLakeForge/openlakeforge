# ADR 0023: the alert evaluator, one run every five minutes. It runs the
# platform API image with another command, so the API and the evaluator are
# released together. Platform phase only (ADR 0002): it reads the stages'
# Dagster and the object store and never project code.
locals {
  enabled = length(var.receivers) > 0
  labels  = { "app.kubernetes.io/name" = var.release_name }

  # ADR 0017's flow rows F38 to F40 select on this label.
  pod_labels = merge(local.labels, { "openlakeforge.io/job" = "alert-evaluator" })

  token_dir = "/var/run/secrets/openlakeforge/platform-api"

  # Named by position: receiver names allow '-', which an environment variable
  # name does not, and two names could otherwise map to one variable. A webhook
  # holds one variable (its URL); a github-discussions receiver two (the App's
  # id and private key), whose Secret keys are fixed by `olf infra github connect`.
  receiver_prefix = { for index, receiver in var.receivers : receiver.name => "OPENLAKEFORGE_ALERT_RECEIVER_${index}" }

  # Environment variable name => the Secret and key it is read from.
  secret_envs = merge(
    { for receiver in var.receivers : local.receiver_prefix[receiver.name] => { name = receiver.secret_name, key = receiver.secret_key } if receiver.type == "webhook" },
    { for receiver in var.receivers : "${local.receiver_prefix[receiver.name]}_APP_ID" => { name = receiver.secret_name, key = "app-id" } if receiver.type == "github-discussions" },
    { for receiver in var.receivers : "${local.receiver_prefix[receiver.name]}_PRIVATE_KEY" => { name = receiver.secret_name, key = "private-key" } if receiver.type == "github-discussions" },
  )

  # The URL and the App's private key are credentials (ADR 0023, decision 4),
  # so this file names the variables that hold them and never a value.
  settings = {
    stages               = var.stages
    dagster_graphql_urls = var.dagster_graphql_urls
    state_base_uri       = var.state_base_uri
    quality_manifests    = var.quality_manifests
    receivers = [
      for receiver in var.receivers : merge(
        {
          name     = receiver.name
          type     = receiver.type
          stages   = receiver.stages
          platform = receiver.platform
        },
        { for key, value in { url_env = local.receiver_prefix[receiver.name] } : key => value if receiver.type == "webhook" },
        { for key, value in {
          repository      = receiver.repository
          category        = receiver.category
          api_url         = receiver.api_url
          app_id_env      = "${local.receiver_prefix[receiver.name]}_APP_ID"
          private_key_env = "${local.receiver_prefix[receiver.name]}_PRIVATE_KEY"
        } : key => value if receiver.type == "github-discussions" },
      )
    ]
    links = {
      dagster_scheme        = var.dagster_scheme
      dagster_host_template = var.dagster_host_template
      runbook_base_url      = var.runbook_base_url
    }
    # The platform API's stage health route (ADR 0023, decision 11), read with the projected token below.
    platform_api_url        = var.platform_api_url
    platform_api_token_file = var.platform_api_url == null ? null : "${local.token_dir}/token"
    s3_endpoint_url         = var.s3_endpoint_url
    s3_region               = var.s3_region
    ca_file                 = var.ca_configmap_name == null ? null : "/ca/ca.crt"
  }
}

# No Role is bound and the default token is not mounted: the evaluator holds no
# Kubernetes API access. Its one credential for the cluster is the projected
# token below, which only the platform API accepts (ADR 0023, decision 7).
resource "kubernetes_service_account_v1" "evaluator" {
  count = local.enabled ? 1 : 0

  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  automount_service_account_token = false
}

resource "kubernetes_config_map_v1" "settings" {
  count = local.enabled ? 1 : 0

  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "alerts.json" = jsonencode(local.settings)
  }
}

resource "kubernetes_cron_job_v1" "evaluator" {
  count = local.enabled ? 1 : 0

  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  lifecycle {
    precondition {
      condition     = var.image != null
      error_message = "image is required when a receiver is declared"
    }
  }

  spec {
    schedule = "*/5 * * * *"
    # One evaluator at a time (ADR 0023, D3): a run still going
    # when the next is due makes that one skipped, not overlapped.
    concurrency_policy            = "Forbid"
    starting_deadline_seconds     = 120
    successful_jobs_history_limit = 1
    failed_jobs_history_limit     = 1

    job_template {
      metadata {
        labels = local.pod_labels
      }

      spec {
        backoff_limit = 0
        # Under the five-minute period, so a hung run never holds the next off.
        # The evaluator stops starting sends after DELIVERY_BUDGET_SECONDS (150,
        # alerting/run.py) so a slow receiver cannot reach this deadline before
        # the state is saved; keep that budget well below this value.
        active_deadline_seconds    = 240
        ttl_seconds_after_finished = 3600

        template {
          metadata {
            labels = local.pod_labels
          }

          spec {
            restart_policy                  = "Never"
            service_account_name            = kubernetes_service_account_v1.evaluator[0].metadata[0].name
            automount_service_account_token = false

            container {
              name              = "evaluator"
              image             = var.image
              image_pull_policy = var.image_pull_policy
              command           = ["python", "-m", "openlakeforge_platform_api.alerting"]

              env {
                name  = "OPENLAKEFORGE_ALERTS_SETTINGS"
                value = "/config/alerts.json"
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
              # The root filesystem is read-only; the combined CA bundle is written
              # to the "tmp" volume.
              env {
                name  = "TMPDIR"
                value = "/tmp"
              }
              dynamic "env" {
                for_each = local.secret_envs
                content {
                  name = env.key
                  value_from {
                    secret_key_ref {
                      name = env.value.name
                      key  = env.value.key
                    }
                  }
                }
              }

              volume_mount {
                name       = "settings"
                mount_path = "/config"
                read_only  = true
              }
              volume_mount {
                name       = "tmp"
                mount_path = "/tmp"
              }
              dynamic "volume_mount" {
                for_each = var.platform_api_url == null ? [] : [1]
                content {
                  name       = "platform-api-token"
                  mount_path = local.token_dir
                  read_only  = true
                }
              }
              dynamic "volume_mount" {
                for_each = var.ca_configmap_name == null ? [] : [1]
                content {
                  name       = "ca"
                  mount_path = "/ca"
                  read_only  = true
                }
              }

              resources {
                requests = {
                  cpu    = "50m"
                  memory = "96Mi"
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
                name = kubernetes_config_map_v1.settings[0].metadata[0].name
              }
            }
            # A token for the platform API alone: the audience keeps the Kubernetes API server and every other service from
            # accepting it, and the kubelet refreshes it well inside its ten minutes.
            dynamic "volume" {
              for_each = var.platform_api_url == null ? [] : [1]
              content {
                name = "platform-api-token"
                projected {
                  sources {
                    service_account_token {
                      audience           = var.platform_api_audience
                      expiration_seconds = 600
                      path               = "token"
                    }
                  }
                }
              }
            }
            volume {
              name = "tmp"
              empty_dir {
                size_limit = "16Mi"
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
  }
}
