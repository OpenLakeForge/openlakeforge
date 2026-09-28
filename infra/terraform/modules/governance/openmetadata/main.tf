locals {
  labels = {
    "app.kubernetes.io/name"       = "openmetadata"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "governance"
  }

  om_url                        = "http://${var.release_name}.${var.namespace}:${var.om_http_port}"
  catalog_schema_names_json     = jsonencode(var.catalog_schema_names)
  catalog_schema_names_json_b64 = base64encode(local.catalog_schema_names_json)
  catalog_type                  = coalesce(try(var.catalog_contract.catalog_type, null), "rest")
  catalog_service_name          = local.catalog_type == "glue" ? "aws_glue" : "polaris"
  catalog_service_display_name  = local.catalog_type == "glue" ? "AWS Glue Data Catalog" : "Polaris Iceberg Catalog"
  catalog_database_name         = var.stages[var.canonical_stage].catalog_database_name
  catalog_database_fqn          = "${local.catalog_service_name}.${local.catalog_database_name}"
  postgresql_ssl_mode           = var.postgresql_ssl_mode != "" ? var.postgresql_ssl_mode : coalesce(try(var.postgresql_contract.ssl_mode, null), "disable")
  storage_secret_env = var.storage_contract.credentials_secret_name == null ? [] : [
    {
      name        = "AWS_ACCESS_KEY_ID"
      secret_name = var.storage_contract.credentials_secret_name
      key         = coalesce(try(var.storage_contract.access_key_id_key, null), "AWS_ACCESS_KEY_ID")
    },
    {
      name        = "AWS_SECRET_ACCESS_KEY"
      secret_name = var.storage_contract.credentials_secret_name
      key         = coalesce(try(var.storage_contract.secret_access_key_key, null), "AWS_SECRET_ACCESS_KEY")
    },
  ]
  polaris_secret_env = try(var.catalog_contract.om_credentials_secret_name, null) == null ? [] : [
    {
      name        = "POLARIS_OM_CLIENT_ID"
      secret_name = var.catalog_contract.om_credentials_secret_name
      key         = coalesce(try(var.catalog_contract.om_client_id_key, null), "POLARIS_OM_CLIENT_ID")
    },
    {
      name        = "POLARIS_OM_CLIENT_SECRET"
      secret_name = var.catalog_contract.om_credentials_secret_name
      key         = coalesce(try(var.catalog_contract.om_client_secret_key, null), "POLARIS_OM_CLIENT_SECRET")
    },
  ]
  bootstrap_secret_env = concat(local.storage_secret_env, local.polaris_secret_env)
  # See the Polaris module: an immutable Job template needs a new name for
  # the ingestion-bot Secret replicas to be created for a newly added stage.
  bootstrap_script = templatefile("${path.module}/templates/bootstrap.sh.tftpl", {
    om_url                        = local.om_url
    admin_email                   = var.admin_email
    admin_password                = var.admin_password
    catalog_type                  = local.catalog_type
    catalog_service_name          = local.catalog_service_name
    catalog_service_display_name  = local.catalog_service_display_name
    catalog_uri                   = coalesce(try(var.catalog_contract.rest_uri, null), try(var.catalog_contract.glue_rest_uri, null), "")
    catalog_warehouse             = coalesce(try(var.catalog_contract.warehouse, null), try(var.catalog_contract.glue_rest_warehouse, null), local.catalog_database_name)
    token_uri                     = (try(var.catalog_contract.token_uri, null) == null ? "" : try(var.catalog_contract.token_uri, null))
    oauth_scope                   = (try(var.catalog_contract.oauth_scope, null) == null ? "" : try(var.catalog_contract.oauth_scope, null))
    ingestion_bot_secret_name     = var.ingestion_bot_secret_name
    trino_lineage_namespace       = var.trino_lineage_namespace
    ingestion_bot_jwt_key         = var.ingestion_bot_jwt_key
    storage_region                = var.storage_contract.region
    storage_endpoint              = (try(var.storage_contract.virtual_host_endpoint, null) == null ? "" : try(var.storage_contract.virtual_host_endpoint, null))
    catalog_database_name         = local.catalog_database_name
    catalog_database_fqn          = local.catalog_database_fqn
    catalog_schema_names_json_b64 = local.catalog_schema_names_json_b64
    stages_json_b64               = base64encode(jsonencode(var.stages))
    lineage_pipeline_service      = var.stages[var.canonical_stage].pipeline_service_name
    superset_username             = var.superset_username
    superset_password             = var.superset_password
    superset_auth_provider        = var.superset_auth_provider
    superset_verify_ssl           = var.superset_verify_ssl
  })

  bootstrap_job_name = "openmetadata-bootstrap-${helm_release.openmetadata.metadata.revision}"

  # Keyed on the whole bootstrap job -- its name and its rendered script --
  # as well as the namespace set. That job mints the credentials being
  # copied, and a Job spec is immutable, so any change to the script
  # replaces it and re-mints them. Keying on the name alone would miss every
  # replacement that keeps the Helm revision, leaving each namespace with a
  # token the service no longer accepts.
  workload_revision = substr(
    sha256(join(",", concat(
      sort(var.workload_namespaces),
      ["revoked"],
      sort(var.revoked_namespaces),
      [local.bootstrap_job_name, sha256(local.bootstrap_script)],
    ))),
    0,
    8,
  )

  bootstrap_annotations = {
    "openlakeforge.io/openmetadata-release-revision" = tostring(helm_release.openmetadata.metadata.revision)
    "openlakeforge.io/catalog-schema-hash"           = sha256(local.catalog_schema_names_json)
  }
}
