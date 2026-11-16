# #1186: each stage's quality reader may Read and List its own stage's
# quality manifests and nothing else, and no stage's key reaches another's.

mock_provider "kubernetes" {}
mock_provider "helm" {}

variables {
  namespace          = "olf-system"
  base_values_file   = "tests/fixtures/values.yaml"
  storage_sizes_file = "tests/fixtures/sizes.yaml"
  bucket_names       = ["ops"]
  region             = "us-east-1"
  quality_reader_credentials = {
    dev = {
      namespace               = "olf-system"
      credentials_secret_name = "platform-api-quality-reader-dev-s3-creds"
      ops_bucket_name         = "ops"
    }
    prod = {
      namespace               = "olf-system"
      credentials_secret_name = "platform-api-quality-reader-prod-s3-creds"
      ops_bucket_name         = "ops"
    }
  }
}

run "each_stage_reads_and_lists_only_its_own_quality_prefix" {
  command = plan

  assert {
    condition = (
      { for i in jsondecode(regex("identities='(.*)'", kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0])[0]) : i.name => i.actions if startswith(i.name, "openlakeforge-platform-api-quality-") }
      == {
        "openlakeforge-platform-api-quality-dev" = [
          "Read:ops/activations/dev/run-artifacts/quality/*",
          "List:ops/activations/dev/run-artifacts/quality/*",
        ]
        "openlakeforge-platform-api-quality-prod" = [
          "Read:ops/activations/prod/run-artifacts/quality/*",
          "List:ops/activations/prod/run-artifacts/quality/*",
        ]
      }
    )
    error_message = "a quality reader must hold Read and List on its own stage's quality prefix only, never Write"
  }

  assert {
    condition     = output.quality_reader_secret_names == { dev = "platform-api-quality-reader-dev-s3-creds", prod = "platform-api-quality-reader-prod-s3-creds" }
    error_message = "the platform API needs each stage's Secret name"
  }
}

run "no_reader_is_minted_unless_asked_for" {
  command = plan

  variables {
    quality_reader_credentials = {}
  }

  assert {
    condition     = length(regexall("openlakeforge-platform-api-quality", kubernetes_job_v1.credentials.spec[0].template[0].spec[0].container[0].args[0])) == 0
    error_message = "a stack without the platform API must not mint a quality key"
  }
}

run "a_stage_name_that_could_widen_the_prefix_is_refused" {
  command = plan

  variables {
    quality_reader_credentials = {
      "dev*" = {
        namespace               = "olf-system"
        credentials_secret_name = "x"
        ops_bucket_name         = "ops"
      }
    }
  }

  expect_failures = [var.quality_reader_credentials]
}
