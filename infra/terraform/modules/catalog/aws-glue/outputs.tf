output "contract" {
  description = "Iceberg catalog contract implemented by AWS Glue Data Catalog."
  value = {
    rest_uri               = local.rest_uri
    token_uri              = null
    warehouse              = var.account_id
    oauth_scope            = null
    catalog_type           = "glue"
    catalog_provider       = "aws-glue"
    catalog_name           = var.catalog_name
    runtime_profile        = "aws-glue-rest"
    trino_catalog_name     = var.trino_catalog_name
    glue_catalog_id        = var.account_id
    glue_region            = var.region
    glue_rest_uri          = local.rest_uri
    glue_rest_warehouse    = var.account_id
    glue_database          = null
    glue_database_location = null
    glue_warehouse_prefix  = "warehouse/iceberg"
    # No glue_database_names/glue_schema_names/catalog_schema_names/
    # catalog_namespaces here: database lifecycle is reconciled in Phase 2
    # (ADR 0002), so Phase 1 has nothing authoritative to report. Omitting the
    # keys -- rather than emitting empty lists -- is what lets
    # olf/contracts.py fall back to the descriptor-derived values instead of
    # exporting an empty JSON array over them.
    endpoint                     = local.rest_uri
    auth_mode                    = "aws-sigv4-pod-identity"
    ssl_mode                     = "required"
    ingress_mode                 = "aws-public-service-endpoint"
    om_credentials_secret_name   = null
    om_client_id_key             = null
    om_client_secret_key         = null
    floe_credentials_secret_name = null
    floe_client_id_key           = null
    floe_client_secret_key       = null
  }
}

output "stage_contracts" {
  description = "Stage-owned Glue bindings for provider-contract construction. Every stage shares the account's one default catalog (glue_catalog_id = account_id) - this account's Glue service refuses to create custom catalogs - so `catalog_name` here is a physical-database-name prefix, not a catalog identity: it is what keeps two stages' otherwise-identical database names collision-free in that one shared catalog."
  value = {
    for stage, binding in local.stage_catalogs : stage => {
      catalog_name        = binding.catalog_name
      trino_catalog_name  = binding.catalog_name
      glue_catalog_id     = var.account_id
      glue_region         = var.region
      glue_rest_uri       = local.rest_uri
      glue_rest_warehouse = var.account_id
      glue_database       = null
      rest_uri            = local.rest_uri
      token_uri           = null
      oauth_scope         = null
      warehouse           = var.account_id
    }
  }
}
