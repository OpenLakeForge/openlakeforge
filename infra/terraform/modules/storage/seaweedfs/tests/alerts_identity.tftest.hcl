# ADR 0023, decision 3: the alert evaluator's key exists only with a receiver,
# and the credentials Job both mints its Secret and may read it.

mock_provider "helm" {}
mock_provider "kubernetes" {}

variables {
  namespace               = "olf-system"
  base_values_file        = "tests/values.yaml"
  storage_sizes_file      = "tests/sizes.yaml"
  bucket_names            = ["ops"]
  region                  = "us-east-1"
  credentials_secret_name = "seaweedfs-s3-creds"
}

run "a_receiver_gets_a_minted_and_readable_key" {
  command = plan

  variables {
    alerts_credentials = {
      namespace               = "olf-system"
      credentials_secret_name = "alert-evaluator-s3-creds"
      ops_bucket_name         = "ops"
      prefix                  = "alerts/"
      quality_prefixes        = ["activations/dev/run-artifacts/quality/"]
    }
  }

  assert {
    condition     = strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "alert-evaluator-s3-creds=olf-system")
    error_message = "the Job mints the alerts Secret"
  }

  assert {
    condition     = anytrue([for rule in kubernetes_role_v1.credentials["olf-system"].rule : contains(coalesce(rule.resource_names, []), "alert-evaluator-s3-creds") && contains(rule.verbs, "get")])
    error_message = "the Job may read the alerts Secret"
  }

  assert {
    condition = (
      strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "Write:ops/alerts/*")
      && strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "Read:ops/activations/dev/run-artifacts/quality/*")
      && !strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "Write:ops/activations/dev/run-artifacts/quality/*")
    )
    error_message = "read-write on its own prefix, read-only on the quality manifests"
  }

  assert {
    condition     = output.alerts_secret_name == "alert-evaluator-s3-creds"
    error_message = "the consumer reads the Secret name through the output"
  }
}

run "no_receiver_means_no_key" {
  command = plan

  assert {
    condition     = !strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "alert-evaluator") && !strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "openlakeforge-alerts")
    error_message = "without a receiver the Job mints no alerts Secret or identity"
  }

  assert {
    condition     = !anytrue([for rule in kubernetes_role_v1.credentials["olf-system"].rule : contains(coalesce(rule.resource_names, []), "alert-evaluator-s3-creds")])
    error_message = "without a receiver the Job may not read it"
  }

  assert {
    condition     = output.alerts_secret_name == null
    error_message = "no Secret name without a receiver"
  }
}
