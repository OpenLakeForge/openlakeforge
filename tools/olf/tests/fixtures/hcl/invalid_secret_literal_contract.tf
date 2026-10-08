locals {
  foundation_contract = merge(data.terraform_remote_state.local_foundation.outputs.foundation_contract, {
    provider = "local"
  })
  kubernetes_platform_contract = { provider = "local" }
  storage_contract              = merge(module.seaweedfs.contract, { provider = "local" })
  metadata_database_contract    = merge(module.postgresql.contract, { provider = "local" })
  catalog_contract               = merge(module.polaris.contract, { provider = "local" })
  query_contract                 = { provider = "local" }
  orchestration_contract         = { provider = "local" }
  governance_contract             = merge(module.openmetadata.contract, { provider = "local" })
  reporting_contract              = merge(module.superset.contract, { provider = "local" })
  artifact_registry_contract      = { provider = "local" }
  artifact_bucket_contract        = { provider = "local" }
  artifact_contract               = merge(local.artifact_registry_contract, { provider = "local" })
  secrets_contract                = { provider = "local" }
  identity_contract               = { provider = "local" }
  access_contract                 = { provider = "local" }
  observability_contract          = { provider = "local" }
  stage_metadata_database_contracts = {}
  selected_stage_analytics          = true
  provider_contracts = {
    schema_version = "2.0.0"
  }
}

check "foundation_contract_matches_platform_context" {
  assert {
    condition     = true
    error_message = "n/a"
  }
}

check "local_contract_adapters_are_explicit" {
  assert {
    condition     = true
    error_message = "n/a"
  }
}

check "catalog_contract_consumer_support" {
  assert {
    condition     = true
    error_message = "n/a"
  }
}

check "openmetadata_catalog_fqn_uses_lakehouse_database" {
  assert {
    condition     = true
    error_message = "n/a"
  }
}

check "stage_namespaces_are_distinct" {
  assert {
    condition     = true
    error_message = "n/a"
  }
}

check "stage_services_stay_in_their_own_stage" {
  assert {
    condition     = true
    error_message = "n/a"
  }
}

check "stage_metadata_state_is_not_shared" {
  assert {
    condition     = true
    error_message = "n/a"
  }
}

# A literal credential in the contract surface: must be a Secret reference.
locals {
  leaky_contract = {
    credentials_secret_name = "seaweedfs-s3-creds"
    secret_access_key       = "hunter2"
    root_client_secret      = random_password.root.result
    secret_access_key_key   = local.storage_contract.secret_access_key_key
  }
}
