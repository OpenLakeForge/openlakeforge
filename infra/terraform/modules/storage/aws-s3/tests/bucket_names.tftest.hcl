# AWS bucket names are not on the `<prefix>-<stage>-<layer>` rule (#993): replacing a bucket deletes its objects
# (force_destroy), so an existing deployment's next apply must plan the same names. This pins them.

mock_provider "aws" {}
mock_provider "random" {}

variables {
  region             = "eu-west-1"
  bucket_name_prefix = "acme-poc"
  stage_buckets = {
    dev  = { bronze_bucket_name = "legacy-bronze", silver_bucket_name = "legacy-silver", gold_bucket_name = "legacy-gold", preserve_legacy_names = true }
    prod = { bronze_bucket_name = "acme-prod-bronze", silver_bucket_name = "acme-prod-silver", gold_bucket_name = "acme-prod-gold" }
  }
}

run "dev_keeps_its_legacy_names_and_other_stages_get_a_suffix" {
  command = apply

  assert {
    condition     = output.stage_contracts["dev"].bronze_bucket_name == "legacy-bronze"
    error_message = "A legacy DEV bucket was renamed."
  }

  assert {
    condition     = startswith(output.stage_contracts["prod"].bronze_bucket_name, "acme-prod-bronze-") && length(output.stage_contracts["prod"].bronze_bucket_name) > length("acme-prod-bronze-")
    error_message = "A non-DEV stage bucket lost its random suffix."
  }

  assert {
    condition     = startswith(output.ops_bucket_name, "acme-poc-ops-")
    error_message = "The ops bucket lost its generated name."
  }
}

run "a_legacy_dev_without_explicit_names_keeps_the_generated_suffix" {
  command = apply

  variables {
    stage_buckets = { dev = { preserve_legacy_names = true } }
  }

  assert {
    condition     = startswith(output.stage_contracts["dev"].silver_bucket_name, "acme-poc-silver-")
    error_message = "A legacy DEV bucket without an explicit name lost its generated name."
  }
}
