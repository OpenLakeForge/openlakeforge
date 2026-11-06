locals {
  labels = {
    "app.kubernetes.io/name"       = "polaris"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "catalog"
  }

  root_client_id = "root"
  realm          = "POLARIS"
  # Namespace-qualified: stage-scoped consumers resolve this from their own
  # namespace, where a bare service name would not resolve.
  rest_uri    = "http://${var.release_name}.${var.namespace}:8181/api/catalog"
  token_uri   = "http://${var.release_name}.${var.namespace}:8181/api/catalog/v1/oauth/tokens"
  oauth_scope = "PRINCIPAL_ROLE:ALL"
  silver_bucket_name = (
    var.storage_contract.silver_bucket_name != null
    ? var.storage_contract.silver_bucket_name
    : var.storage_contract.bucket_name
  )
  # `tomap(...)`, not a bare object literal: an unwrapped `{ dev = {...} }`
  # infers as object({dev = object({...})}), which does not reliably unify
  # with var.stage_catalogs's map(object(...)) type once the branch values
  # are known-after-apply (module.seaweedfs.contract, etc.) - this fails at
  # `terraform apply` with "Inconsistent conditional result types" even
  # though `terraform validate` accepts it against unknown placeholder values.
  stage_catalogs = length(var.stage_catalogs) > 0 ? var.stage_catalogs : tomap({
    dev = {
      catalog_name                     = var.catalog_name
      trino_credentials_secret_name    = var.trino_credentials_secret_name
      floe_credentials_secret_name     = var.floe_credentials_secret_name
      deployer_credentials_secret_name = var.deployer_credentials_secret_name
    }
  })

  metastore_bootstrap_annotations = {
    "openlakeforge.io/polaris-release-revision" = tostring(helm_release.polaris.metadata.revision)
  }
}
