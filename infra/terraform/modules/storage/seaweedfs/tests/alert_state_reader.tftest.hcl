# #1258: the platform API's alert state key may Read and List the evaluator's
# state prefix and nothing else: no Write, no history, no other prefix.

mock_provider "kubernetes" {}
mock_provider "helm" {}

variables {
  namespace          = "olf-system"
  base_values_file   = "tests/fixtures/values.yaml"
  storage_sizes_file = "tests/fixtures/sizes.yaml"
  bucket_names       = ["ops"]
  region             = "us-east-1"
  alert_state_reader_credentials = {
    namespace               = "olf-system"
    credentials_secret_name = "platform-api-alert-state-s3-creds"
    ops_bucket_name         = "ops"
    prefix                  = "alerts/state/"
  }
}

run "the_key_reads_and_lists_only_the_state_prefix" {
  command = plan

  assert {
    condition = (
      { for i in jsondecode(regex("identities='(.*)'", kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0])[0]) : i.name => i.actions if i.name == "openlakeforge-platform-api-alerts" }
      == { "openlakeforge-platform-api-alerts" = ["Read:ops/alerts/state/*", "List:ops/alerts/state/*"] }
    )
    error_message = "the alert state key must hold Read and List on the state prefix only, never Write"
  }

  assert {
    condition     = strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "platform-api-alert-state-s3-creds=olf-system")
    error_message = "the Job mints the alert state Secret"
  }

  assert {
    condition     = anytrue([for rule in kubernetes_role_v1.credentials["olf-system"].rule : contains(coalesce(rule.resource_names, []), "platform-api-alert-state-s3-creds") && contains(rule.verbs, "get")])
    error_message = "the Job may read the alert state Secret"
  }

  assert {
    condition     = output.alert_state_reader_secret_name == "platform-api-alert-state-s3-creds"
    error_message = "the platform API reads the Secret name through the output"
  }
}

run "no_key_is_minted_unless_asked_for" {
  command = plan

  variables {
    alert_state_reader_credentials = null
  }

  assert {
    condition     = !strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "openlakeforge-platform-api-alerts")
    error_message = "without the platform API and the evaluator the Job mints no alert state identity"
  }

  assert {
    condition     = output.alert_state_reader_secret_name == null
    error_message = "no Secret name without the key"
  }
}

run "a_prefix_that_could_widen_the_grant_is_refused" {
  command = plan

  variables {
    alert_state_reader_credentials = {
      namespace               = "olf-system"
      credentials_secret_name = "x"
      ops_bucket_name         = "ops"
      prefix                  = "alerts/state"
    }
  }

  expect_failures = [var.alert_state_reader_credentials]
}
