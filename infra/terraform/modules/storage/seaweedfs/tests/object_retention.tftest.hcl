# ADR 0022, decision 6: archive retention is a SeaweedFS filer path TTL per key prefix, set by a Job after the
# bucket exists and verified against the filer's own configuration.

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

run "no_retention_creates_no_job" {
  command = plan

  assert {
    condition     = length(kubernetes_job_v1.object_retention) == 0
    error_message = "without object_retention no retention Job exists"
  }
}

run "each_prefix_gets_a_ttl_and_is_checked" {
  command = plan

  variables {
    object_retention = {
      bucket   = "ops"
      days     = 30
      prefixes = ["activations/dev/logs/v2/", "platform/logs/v2/"]
    }
  }

  assert {
    condition = (
      strcontains(kubernetes_job_v1.object_retention[0].spec[0].template[0].spec[0].container[0].args[0], "fs.configure -locationPrefix=/buckets/ops/activations/dev/logs/v2/ -ttl=30d -apply")
      && strcontains(kubernetes_job_v1.object_retention[0].spec[0].template[0].spec[0].container[0].args[0], "fs.configure -locationPrefix=/buckets/ops/platform/logs/v2/ -ttl=30d -apply")
    )
    error_message = "every prefix gets a path rule with the requested number of days"
  }

  assert {
    condition     = strcontains(kubernetes_job_v1.object_retention[0].spec[0].template[0].spec[0].container[0].args[0], "No 30d retention rule for platform/logs/v2/")
    error_message = "the Job fails when a rule is not in the filer's configuration afterwards"
  }
}

run "a_prefix_must_end_in_a_slash" {
  command = plan

  variables {
    object_retention = {
      bucket   = "ops"
      days     = 30
      prefixes = ["activations/dev/logs/v2"]
    }
  }

  expect_failures = [var.object_retention]
}

run "days_are_a_whole_number_of_at_least_one" {
  command = plan

  variables {
    object_retention = {
      bucket   = "ops"
      days     = 0
      prefixes = ["activations/dev/logs/v2/"]
    }
  }

  expect_failures = [var.object_retention]
}

# ADR 0017: SeaweedFS admits only the pods a flow row of release/network-flows.yaml names, so the Job's pod labels
# must contain every label of a row's source that reaches the master (9333) and the filer (8888).
run "the_job_is_admitted_to_the_master_and_filer" {
  command = plan

  variables {
    object_retention = {
      bucket   = "ops"
      days     = 30
      prefixes = ["activations/dev/logs/v2/"]
    }
  }

  assert {
    condition = anytrue([
      for flow in yamldecode(file("${path.module}/../../../../../release/network-flows.yaml")).flows :
      length(try(flow.from.pods, {})) > 0
      && alltrue([for k, v in try(flow.from.pods, {}) : lookup(kubernetes_job_v1.object_retention[0].spec[0].template[0].metadata[0].labels, k, null) == v])
      && try(flow.to.pods["app.kubernetes.io/name"], "") == "seaweedfs"
      && contains(flow.ports, 9333) && contains(flow.ports, 8888)
    ])
    error_message = "no flow row admits the retention Job's pods to the SeaweedFS master and filer"
  }
}
