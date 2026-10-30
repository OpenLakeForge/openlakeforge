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
  bronze_bucket_name = (
    var.storage_contract.bronze_bucket_name != null
    ? var.storage_contract.bronze_bucket_name
    : var.storage_contract.bucket_name
  )
  silver_bucket_name = (
    var.storage_contract.silver_bucket_name != null
    ? var.storage_contract.silver_bucket_name
    : var.storage_contract.bucket_name
  )
  gold_bucket_name = (
    var.storage_contract.gold_bucket_name != null
    ? var.storage_contract.gold_bucket_name
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
      namespace                        = var.namespace
      catalog_name                     = var.catalog_name
      bronze_bucket_name               = local.bronze_bucket_name
      silver_bucket_name               = local.silver_bucket_name
      gold_bucket_name                 = local.gold_bucket_name
      trino_principal_name             = var.principal_name
      trino_credentials_secret_name    = var.trino_credentials_secret_name
      floe_principal_name              = var.floe_principal_name
      floe_credentials_secret_name     = var.floe_credentials_secret_name
      deployer_principal_name          = var.deployer_principal_name
      deployer_credentials_secret_name = var.deployer_credentials_secret_name
    }
  })
  # Stage catalogs vend credentials only when the storage adapter offers an STS.
  catalog_sts = (var.storage_contract.sts_role_arn == null ? null : {
    role_arn     = var.storage_contract.sts_role_arn
    sts_endpoint = var.storage_contract.sts_endpoint
    endpoint     = var.storage_contract.endpoint
    region       = var.storage_contract.region
  })
  # The Job template is immutable, so a changed workload-namespace set has to
  # produce a new Job name for the credential replicas to be created.
  bootstrap_script = templatefile("${path.module}/templates/bootstrap.sh.tftpl", {
    release_name               = var.release_name
    oauth_scope                = local.oauth_scope
    stage_catalogs             = local.stage_catalogs
    sts                        = local.catalog_sts
    principal_role             = var.principal_role
    catalog_role               = var.catalog_role
    floe_principal_role        = var.floe_principal_role
    floe_catalog_role          = var.floe_catalog_role
    om_principal_name          = var.om_principal_name
    om_credentials_secret_name = var.om_credentials_secret_name
    om_principal_role          = var.om_principal_role
    om_catalog_role            = var.om_catalog_role
    deployer_principal_role    = var.deployer_principal_role
    deployer_catalog_role      = var.deployer_catalog_role
  })

  bootstrap_job_name = "polaris-bootstrap-${helm_release.polaris.metadata.revision}"

  # Keyed on the whole bootstrap job -- its name and its rendered script --
  # as well as the namespace set. That job mints the credentials being
  # copied, and a Job spec is immutable, so any change to the script
  # replaces it and re-mints them. Keying on the name alone would miss every
  # replacement that keeps the Helm revision, leaving each namespace with a
  # token the service no longer accepts.
  workload_revision = substr(
    sha256(join(",", concat(sort(var.workload_namespaces), [local.bootstrap_job_name, sha256(local.bootstrap_script)]))),
    0,
    8,
  )

  bootstrap_annotations = {
    "openlakeforge.io/polaris-release-revision" = tostring(helm_release.polaris.metadata.revision)
    "openlakeforge.io/bootstrap-revision"       = var.bootstrap_revision
  }
}
