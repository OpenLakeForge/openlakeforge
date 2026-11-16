# ADR 0023: the evaluator exists only with a receiver, runs bounded and
# unprivileged, and never holds a receiver URL as a value.

mock_provider "kubernetes" {}

variables {
  namespace               = "olf-system"
  image                   = "openlakeforge/platform-api:local-abc"
  stages                  = ["dev", "prod"]
  state_base_uri          = "s3://ops/alerts/"
  s3_endpoint_url         = "http://seaweedfs-s3.olf-system:8333"
  s3_region               = "us-east-1"
  credentials_secret_name = "alert-evaluator-s3-creds"
  dagster_graphql_urls = {
    dev  = "http://dagster-dagster-webserver.olf-dev:80/graphql"
    prod = "http://dagster-dagster-webserver.olf-prod:80/graphql"
  }
}

run "no_receivers_means_nothing_is_created" {
  command = plan

  variables {
    receivers = []
  }

  assert {
    condition     = length(kubernetes_cron_job_v1.evaluator) == 0 && length(kubernetes_service_account_v1.evaluator) == 0 && length(kubernetes_config_map_v1.settings) == 0
    error_message = "without a receiver there is no CronJob, ServiceAccount or settings"
  }
}

run "a_receiver_is_referenced_never_valued" {
  command = plan

  variables {
    receivers = [{ name = "ops", secret_name = "ops-webhook", secret_key = "webhook-url", stages = ["prod"] }]
  }

  assert {
    condition = anytrue([
      for e in kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env :
      e.name == "OPENLAKEFORGE_ALERT_RECEIVER_0" && e.value == null
      && e.value_from[0].secret_key_ref[0].name == "ops-webhook" && e.value_from[0].secret_key_ref[0].key == "webhook-url"
    ])
    error_message = "the receiver's URL reaches the pod through secretKeyRef to the declared name and key"
  }

  assert {
    condition     = !strcontains(kubernetes_config_map_v1.settings[0].data["alerts.json"], "https://") && strcontains(kubernetes_config_map_v1.settings[0].data["alerts.json"], "\"url_env\":\"OPENLAKEFORGE_ALERT_RECEIVER_0\"")
    error_message = "the settings name the variable and carry no URL"
  }
}

run "the_job_is_bounded_and_unprivileged" {
  command = plan

  variables {
    receivers = [{ name = "ops", secret_name = "ops-webhook", secret_key = "webhook-url" }]
  }

  assert {
    condition = (
      kubernetes_cron_job_v1.evaluator[0].spec[0].schedule == "*/5 * * * *"
      && kubernetes_cron_job_v1.evaluator[0].spec[0].concurrency_policy == "Forbid"
      && kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].active_deadline_seconds == 240
      && kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].backoff_limit == 0
    )
    error_message = "every five minutes, one at a time, bounded under the period, no retries"
  }

  assert {
    condition = (
      kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].automount_service_account_token == false
      && kubernetes_service_account_v1.evaluator[0].automount_service_account_token == false
      && kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].run_as_non_root == true
      && kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].read_only_root_filesystem == true
      && kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "192Mi"
    )
    error_message = "no API token, non-root, read-only root filesystem and a memory limit"
  }
}

run "the_local_ca_is_mounted_only_when_given" {
  command = plan

  variables {
    receivers         = [{ name = "ops", secret_name = "ops-webhook", secret_key = "webhook-url" }]
    ca_configmap_name = "olf-local-ca"
  }

  assert {
    condition     = strcontains(kubernetes_config_map_v1.settings[0].data["alerts.json"], "\"ca_file\":\"/ca/ca.crt\"") && length(kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].volume) == 3
    error_message = "the CA is mounted and named in the settings"
  }

  assert {
    condition = (
      anytrue([for m in kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].volume_mount : m.name == "ca" && m.mount_path == "/ca" && m.read_only == true])
      && anytrue([for m in kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].volume_mount : m.name == "tmp" && m.mount_path == "/tmp"])
      && anytrue([for v in kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].volume : v.name == "tmp" && length(v.empty_dir) == 1])
      && anytrue([for e in kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env : e.name == "TMPDIR" && e.value == "/tmp"])
    )
    error_message = "the combined CA bundle is written to a writable emptyDir at TMPDIR, since the root filesystem is read-only"
  }
}

run "a_stage_without_a_url_is_refused" {
  command = plan

  variables {
    receivers            = [{ name = "ops", secret_name = "ops-webhook", secret_key = "webhook-url" }]
    dagster_graphql_urls = { dev = "http://x/graphql" }
  }

  expect_failures = [var.dagster_graphql_urls]
}

run "quality_manifests_are_read_with_the_evaluators_own_key" {
  command = plan

  variables {
    receivers = [{ name = "ops", secret_name = "ops-webhook", secret_key = "webhook-url" }]
    quality_manifests = {
      dev  = "s3://ops/activations/dev/"
      prod = "s3://ops/activations/prod/"
    }
  }

  assert {
    condition = (
      jsondecode(kubernetes_config_map_v1.settings[0].data["alerts.json"]).quality_manifests.dev == "s3://ops/activations/dev/"
      && jsondecode(kubernetes_config_map_v1.settings[0].data["alerts.json"]).quality_manifests.prod == "s3://ops/activations/prod/"
    )
    error_message = "the settings carry each stage's activation prefix"
  }

  assert {
    condition = alltrue([
      for e in kubernetes_cron_job_v1.evaluator[0].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env :
      !startswith(e.name, "OPENLAKEFORGE_QUALITY_")
      && (e.value_from == null || length(e.value_from) == 0 || e.value_from[0].secret_key_ref[0].name != "platform-api-quality-reader-dev-s3-creds")
    ])
    error_message = "no per-stage quality key is mounted: the evaluator's own key already reads the quality prefixes"
  }
}

run "quality_manifests_default_to_none" {
  command = plan

  variables {
    receivers = [{ name = "ops", secret_name = "ops-webhook", secret_key = "webhook-url" }]
  }

  assert {
    condition     = length(jsondecode(kubernetes_config_map_v1.settings[0].data["alerts.json"]).quality_manifests) == 0
    error_message = "a deployment without manifest prefixes raises no quality alerts"
  }
}

run "a_manifest_prefix_for_a_stage_that_is_not_enabled_is_refused" {
  command = plan

  variables {
    receivers         = [{ name = "ops", secret_name = "ops-webhook", secret_key = "webhook-url" }]
    quality_manifests = { staging = "s3://ops/activations/staging/" }
  }

  expect_failures = [var.quality_manifests]
}
