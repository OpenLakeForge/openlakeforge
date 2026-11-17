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
