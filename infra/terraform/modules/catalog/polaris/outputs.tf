output "contract" {
  description = "Polaris REST catalog contract for platform consumers."
  value = {
    catalog_type               = "rest"
    catalog_provider           = "polaris"
    catalog_name               = var.catalog_name
    runtime_profile            = "polaris-rest"
    trino_catalog_name         = "iceberg"
    default_warehouse_location = "s3://${local.silver_bucket_name}"
    # No catalog_namespaces/catalog_namespace_names/catalog_schema_names here:
    # namespaces are reconciled in Phase 2 (ADR 0002), so Phase 1 has nothing
    # authoritative to report. Omitting the keys — rather than emitting empty
    # lists — is what lets olf/contracts.py fall back to the descriptor-derived
    # values instead of exporting an empty JSON array over them.
    rest_uri                      = local.rest_uri
    token_uri                     = local.token_uri
    warehouse                     = var.catalog_name
    oauth_scope                   = local.oauth_scope
    trino_credentials_secret_name = var.trino_credentials_secret_name
    trino_client_id_key           = "POLARIS_TRINO_CLIENT_ID"
    trino_client_secret_key       = "POLARIS_TRINO_CLIENT_SECRET"
    floe_credentials_secret_name  = var.floe_credentials_secret_name
    floe_client_id_key            = "POLARIS_FLOE_CLIENT_ID"
    floe_client_secret_key        = "POLARIS_FLOE_CLIENT_SECRET"
    bootstrap_run_id              = kubernetes_job_v1.bootstrap.metadata[0].name
    om_credentials_secret_name    = var.om_credentials_secret_name
    om_client_id_key              = "POLARIS_OM_CLIENT_ID"
    om_client_secret_key          = "POLARIS_OM_CLIENT_SECRET"

    deployer_credentials_secret_name = var.deployer_credentials_secret_name
    deployer_client_id_key           = "POLARIS_DEPLOYER_CLIENT_ID"
    deployer_client_secret_key       = "POLARIS_DEPLOYER_CLIENT_SECRET"
  }

  depends_on = [
    kubernetes_job_v1.bootstrap,
  ]
}

output "stage_contracts" {
  description = "Stage-owned catalog bindings written by the shared Polaris bootstrap job."
  value = {
    for stage, binding in local.stage_catalogs : stage => {
      catalog_name                     = binding.catalog_name
      trino_catalog_name               = binding.catalog_name
      warehouse                        = binding.catalog_name
      rest_uri                         = local.rest_uri
      token_uri                        = local.token_uri
      oauth_scope                      = local.oauth_scope
      trino_credentials_secret_name    = binding.trino_credentials_secret_name
      trino_client_id_key              = "POLARIS_TRINO_CLIENT_ID"
      trino_client_secret_key          = "POLARIS_TRINO_CLIENT_SECRET"
      floe_credentials_secret_name     = binding.floe_credentials_secret_name
      floe_client_id_key               = "POLARIS_FLOE_CLIENT_ID"
      floe_client_secret_key           = "POLARIS_FLOE_CLIENT_SECRET"
      deployer_credentials_secret_name = binding.deployer_credentials_secret_name
      deployer_client_id_key           = "POLARIS_DEPLOYER_CLIENT_ID"
      deployer_client_secret_key       = "POLARIS_DEPLOYER_CLIENT_SECRET"
      bootstrap_run_id                 = kubernetes_job_v1.bootstrap.metadata[0].name
    }
  }
}
