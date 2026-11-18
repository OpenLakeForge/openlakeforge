# ADR 0007: each governed stage's quality reconciler may read its own stage's
# quality manifests and read and write its own `quality-sync/` state, and
# nothing else. No stage's key reaches another's, and a stack without
# governance mints none.

mock_provider "kubernetes" {}
mock_provider "helm" {}

variables {
  namespace          = "olf-system"
  base_values_file   = "tests/fixtures/values.yaml"
  storage_sizes_file = "tests/fixtures/sizes.yaml"
  bucket_names       = ["ops"]
  region             = "us-east-1"
  quality_sync_credentials = {
    dev = {
      namespace               = "olf-system"
      credentials_secret_name = "quality-reconciler-dev-s3-creds"
      ops_bucket_name         = "ops"
    }
    prod = {
      namespace               = "olf-system"
      credentials_secret_name = "quality-reconciler-prod-s3-creds"
      ops_bucket_name         = "ops"
    }
  }
}

run "each_stage_writes_only_its_own_quality_sync_prefix" {
  command = plan

  assert {
    condition = (
      { for i in jsondecode(regex("identities='(.*)'", kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0])[0]) : i.name => i.actions if startswith(i.name, "openlakeforge-quality-sync-") }
      == {
        "openlakeforge-quality-sync-dev" = [
          "Read:ops/activations/dev/run-artifacts/quality/*",
          "List:ops/activations/dev/run-artifacts/quality/*",
          "Read:ops/activations/dev/quality-sync/*",
          "Write:ops/activations/dev/quality-sync/*",
          "List:ops/activations/dev/quality-sync/*",
        ]
        "openlakeforge-quality-sync-prod" = [
          "Read:ops/activations/prod/run-artifacts/quality/*",
          "List:ops/activations/prod/run-artifacts/quality/*",
          "Read:ops/activations/prod/quality-sync/*",
          "Write:ops/activations/prod/quality-sync/*",
          "List:ops/activations/prod/quality-sync/*",
        ]
      }
    )
    error_message = "a reconciler key reads its stage's manifests and writes only its stage's quality-sync prefix, never the manifests"
  }

  assert {
    condition = alltrue([
      for i in jsondecode(regex("identities='(.*)'", kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0])[0]) :
      !anytrue([for action in i.actions : startswith(action, "Write:") && !strcontains(action, "/quality-sync/")])
      if startswith(i.name, "openlakeforge-quality-sync-")
    ])
    error_message = "a reconciler key must hold Write on its quality-sync prefix alone"
  }

  assert {
    condition     = output.quality_sync_secret_names == { dev = "quality-reconciler-dev-s3-creds", prod = "quality-reconciler-prod-s3-creds" }
    error_message = "the reconciler needs each stage's Secret name"
  }
}

run "the_job_mints_each_stage_secret_in_the_namespace_that_reads_it" {
  command = plan

  assert {
    condition     = strcontains(kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0], "quality-reconciler-dev-s3-creds=olf-system")
    error_message = "the credentials Job must create the reconciler's Secret in its own namespace"
  }
}

run "no_key_is_minted_unless_a_stage_is_governed" {
  command = plan

  variables {
    quality_sync_credentials = {}
  }

  assert {
    condition     = length(regexall("openlakeforge-quality-sync", kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0])) == 0
    error_message = "a stack without governance must not mint a quality sync key"
  }
}

run "a_stage_name_that_could_widen_the_prefix_is_refused" {
  command = plan

  variables {
    quality_sync_credentials = {
      "dev*" = {
        namespace               = "olf-system"
        credentials_secret_name = "x"
        ops_bucket_name         = "ops"
      }
    }
  }

  expect_failures = [var.quality_sync_credentials]
}
