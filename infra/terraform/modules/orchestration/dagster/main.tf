locals {
  catalog_type       = coalesce(try(var.catalog_contract.catalog_type, null), "rest")
  catalog_provider   = coalesce(try(var.catalog_contract.catalog_provider, null), "polaris")
  catalog_name       = coalesce(try(var.catalog_contract.catalog_name, null), try(var.catalog_contract.warehouse, null), "lakehouse_dev")
  runtime_profile    = coalesce(try(var.catalog_contract.runtime_profile, null), "polaris-rest")
  dbt_profile_env    = local.catalog_type == "glue" && local.catalog_provider == "aws-glue" ? "aws" : (try(var.storage_contract.provider, null) == "azure" ? "azure" : "local")
  governance_enabled = try(var.governance_contract.enabled, true)

  storage_env = concat(
    [
      {
        name  = "AWS_REGION"
        value = var.storage_contract.region
      },
      {
        name  = "AWS_DEFAULT_REGION"
        value = var.storage_contract.region
      },
    ],
    var.storage_contract.endpoint == null ? [] : [
      {
        name  = "AWS_ENDPOINT_URL_S3"
        value = var.storage_contract.endpoint
      },
    ],
    var.storage_contract.path_style_access == null ? [] : [
      {
        name  = "AWS_S3_FORCE_PATH_STYLE"
        value = tostring(var.storage_contract.path_style_access)
      },
    ],
    try(var.storage_contract.ssl_mode, null) == "disabled" ? [
      {
        name  = "AWS_ALLOW_HTTP"
        value = "true"
      },
    ] : [],
    [
      {
        name  = "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"
        value = coalesce(var.storage_contract.bronze_bucket_name, var.storage_contract.bucket_name)
      },
      {
        name  = "OPENLAKEFORGE_STORAGE_SILVER_BUCKET"
        value = coalesce(try(var.storage_contract.silver_bucket_name, null), "lakehouse-silver")
      },
      {
        name  = "OPENLAKEFORGE_STORAGE_GOLD_BUCKET"
        value = coalesce(try(var.storage_contract.gold_bucket_name, null), "lakehouse-gold")
      },
      {
        name  = "OPENLAKEFORGE_STORAGE_BUCKET"
        value = coalesce(var.storage_contract.bronze_bucket_name, var.storage_contract.bucket_name)
      },
      {
        name  = "OPENLAKEFORGE_STORAGE_OPS_BUCKET"
        value = coalesce(try(var.storage_contract.ops_bucket_name, null), var.artifact_bucket_name)
      },
      {
        name  = "OPENLAKEFORGE_BRONZE_BUCKET"
        value = coalesce(var.storage_contract.bronze_bucket_name, var.storage_contract.bucket_name)
      },
    ],
  )

  artifact_env = [
    {
      name  = "OPENLAKEFORGE_OPS_BUCKET_NAME"
      value = var.artifact_bucket_name
    },
    {
      name  = "OPENLAKEFORGE_ARTIFACT_BUCKET_NAME"
      value = var.artifact_bucket_name
    },
    {
      name  = "OPENLAKEFORGE_ARTIFACT_BASE_URI"
      value = var.artifact_base_uri
    },
    {
      name  = "OPENLAKEFORGE_FLOE_MANIFEST_ACCESS_MODE"
      value = var.floe_manifest_access_mode
    },
    {
      name  = "OPENLAKEFORGE_FLOE_MANIFEST_BASE_URI"
      value = var.floe_manifest_base_uri
    },
    {
      name  = "OPENLAKEFORGE_FLOE_REPORT_BASE_URI"
      value = var.floe_report_base_uri
    },
    {
      name  = "OPENLAKEFORGE_LOG_BASE_URI"
      value = var.log_base_uri
    },
    {
      name  = "OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI"
      value = var.run_artifact_base_uri
    },
    {
      name  = "OPENLAKEFORGE_FLOE_MANIFEST_REVISION"
      value = var.floe_manifest_revision
    },
    {
      name  = "OPENLAKEFORGE_PROJECT_CODE_REVISION"
      value = var.project_code_image_revision
    },
  ]

  generic_catalog_env = [
    {
      name  = "OPENLAKEFORGE_CATALOG_TYPE"
      value = local.catalog_type
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_PROVIDER"
      value = local.catalog_provider
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_NAME"
      value = local.catalog_name
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_RUNTIME_PROFILE"
      value = local.runtime_profile
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_REST_URI"
      value = (try(var.catalog_contract.rest_uri, null) == null ? "" : try(var.catalog_contract.rest_uri, null))
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_TOKEN_URI"
      value = (try(var.catalog_contract.token_uri, null) == null ? "" : try(var.catalog_contract.token_uri, null))
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_WAREHOUSE"
      value = local.catalog_type == "glue" ? local.catalog_name : coalesce(try(var.catalog_contract.warehouse, null), local.catalog_name)
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_OAUTH_SCOPE"
      value = (try(var.catalog_contract.oauth_scope, null) == null ? "" : try(var.catalog_contract.oauth_scope, null))
    },
  ]

  glue_catalog_env = local.catalog_type == "glue" ? [
    {
      name  = "OPENLAKEFORGE_CATALOG_GLUE_REGION"
      value = coalesce(try(var.catalog_contract.glue_region, null), var.storage_contract.region)
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID"
      value = (try(var.catalog_contract.glue_catalog_id, null) == null ? "" : try(var.catalog_contract.glue_catalog_id, null))
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_GLUE_REST_URI"
      value = coalesce(try(var.catalog_contract.glue_rest_uri, null), try(var.catalog_contract.rest_uri, null), "")
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_GLUE_REST_WAREHOUSE"
      value = coalesce(try(var.catalog_contract.glue_rest_warehouse, null), try(var.catalog_contract.warehouse, null), "")
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_GLUE_DATABASE"
      value = (try(var.catalog_contract.glue_database, null) == null ? "" : try(var.catalog_contract.glue_database, null))
    },
    {
      name  = "OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX"
      value = coalesce(try(var.catalog_contract.glue_warehouse_prefix, null), "warehouse/iceberg")
    },
    {
      # This account's shared default Glue catalog has no per-stage catalog
      # (#114 AWS fallback), so physical Gold schema names carry this same
      # stage prefix as Silver's databases. dbt's Gold profile template
      # (libs/dbt/profiles/aws.yml) is baked into the project-code image at
      # BUILD time, provider/stage-agnostic ("one image digest deploys to
      # every stage"), and reads this at dbt-invoke time via `env_var(...)`
      # to prefix its build-time-baked bare schema name.
      name  = "OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX"
      value = "${local.catalog_name}_"
    },
    {
      name  = "OPENLAKEFORGE_DBT_TARGET"
      value = "aws_runtime"
    },
    ] : [
    {
      name  = "OPENLAKEFORGE_DBT_TARGET"
      value = "local_runtime"
    },
  ]

  polaris_catalog_env = local.catalog_type == "rest" ? [
    {
      name  = "POLARIS_REST_URI"
      value = (try(var.catalog_contract.rest_uri, null) == null ? "" : try(var.catalog_contract.rest_uri, null))
    },
    {
      name  = "POLARIS_TOKEN_URI"
      value = (try(var.catalog_contract.token_uri, null) == null ? "" : try(var.catalog_contract.token_uri, null))
    },
    {
      name  = "POLARIS_WAREHOUSE"
      value = coalesce(try(var.catalog_contract.warehouse, null), local.catalog_name)
    },
    {
      name  = "POLARIS_OAUTH_SCOPE"
      value = (try(var.catalog_contract.oauth_scope, null) == null ? "" : try(var.catalog_contract.oauth_scope, null))
    },
  ] : []

  # dbt and every other Trino client resolve the query service from here.
  # `profiles.yml` falls back to a bare service name, which only resolves
  # inside the query service's own namespace - never from a stage namespace.
  query_host = (
    try(var.query_contract.service_namespace, null) == null
    ? var.query_contract.service_name
    : "${var.query_contract.service_name}.${var.query_contract.service_namespace}"
  )

  query_env = [
    {
      name  = "OPENLAKEFORGE_QUERY_TRINO_HOST"
      value = local.query_host
    },
    {
      name  = "OPENLAKEFORGE_QUERY_TRINO_PORT"
      value = tostring(var.query_contract.http_port)
    },
    {
      name  = "OPENLAKEFORGE_QUERY_TRINO_CATALOG"
      value = var.query_contract.catalog_name
    },
  ]

  dbt_env = concat([
    {
      name  = "OPENLAKEFORGE_DBT_PROFILE_ENV"
      value = local.dbt_profile_env
    },
    {
      name  = "DBT_PROFILES_DIR"
      value = "/tmp/openlakeforge-dbt-profiles"
    },
    {
      name  = "OPENLAKEFORGE_DBT_EXECUTABLE"
      value = "dbt-ol"
    },
    {
      name  = "OPENLAKEFORGE_DBT_TRINO_USER"
      value = coalesce(try(var.query_contract.runtime_identity_principal, null), "openlakeforge-dbt")
    },
    {
      name  = "OPENLAKEFORGE_POSTGRES_SSL_MODE"
      value = var.postgresql_ssl_mode
    },
    ], local.governance_enabled ? [
    {
      name = "OPENLINEAGE_URL"
      # The contract's endpoint, not a bare service name: governance is a
      # shared service and Dagster no longer runs beside it.
      value = coalesce(
        try(var.governance_contract.endpoint, null),
        "http://${try(var.governance_contract.service_name, "openmetadata")}:${try(var.governance_contract.http_port, 8585)}",
      )
    },
    {
      name  = "OPENLINEAGE_ENDPOINT"
      value = "api/v1/openlineage/lineage"
    },
    {
      name  = "OPENLINEAGE_NAMESPACE"
      value = "dagster"
    },
  ] : [])

  dbt_secret_env = local.governance_enabled ? [
    {
      name = "OPENLINEAGE_API_KEY"
      valueFrom = {
        secretKeyRef = {
          name = var.governance_contract.ingestion_bot_secret_name
          key  = var.governance_contract.ingestion_bot_jwt_key
        }
      }
    },
  ] : []

  # Floe's Kubernetes job runner (floe_dagster.kubernetes_runner) submits
  # its ephemeral Jobs into this namespace, defaulting to "lakehouse" -
  # OpenLakeForge's own pre-#133 single-stage namespace - when unset. That
  # coincidence masked this gap until stage namespaces became "olf-<stage>"
  # (#133/#114): without it, every stage's Floe run fails RBAC-denied
  # trying to create Jobs in a namespace that no longer exists.
  namespace_env = [
    {
      name  = "NAMESPACE"
      value = var.namespace
    },
    {
      name  = "OPENLAKEFORGE_KUBE_NAMESPACE"
      value = var.namespace
    },
  ]

  # Conditioning the *source* list rather than the rendered result keeps both
  # branches the same type. A `? <objects> : {}` on the result is what raised
  # "Inconsistent conditional result types": Terraform cannot unify an object
  # that has attributes with one that has none, and `terraform validate` does
  # not evaluate it -- only a real apply does.
  managed_code_locations   = var.manage_user_deployments ? var.code_locations : []
  workspace_code_locations = var.manage_user_deployments ? [] : var.code_locations

  workspace_servers = [
    for location in local.workspace_code_locations : {
      host = location.name
      port = 3030
      name = location.name
    }
  ]

  code_location_deployments = [
    for location in local.managed_code_locations : {
      name = location.name
      image = {
        repository = var.project_code_image_repository
        tag        = var.project_code_image_tag
        pullPolicy = var.project_code_image_pull_policy
      }
      dagsterApiGrpcArgs = [
        "--module-name",
        location.definitions_module,
      ]
      port = 3030
      includeConfigInLaunchedRuns = {
        enabled = true
      }
      deploymentConfig = {
        strategy = {
          type = "Recreate"
        }
      }
      podSpecConfig = {
        terminationGracePeriodSeconds = 10
      }
      env        = local.runtime_env
      envSecrets = local.runtime_env_secrets
    }
  ]

  runtime_env = concat(local.storage_env, local.artifact_env, local.generic_catalog_env, local.glue_catalog_env, local.polaris_catalog_env, local.query_env, local.dbt_env, local.dbt_secret_env, local.namespace_env)

  log_archive_env = concat(
    local.storage_env,
    local.artifact_env,
    [
      {
        name  = "OPENLAKEFORGE_KUBE_NAMESPACE"
        value = var.namespace
      },
      {
        name  = "OPENLAKEFORGE_LOG_ARCHIVE_SINCE_SECONDS"
        value = "3600"
      },
    ],
  )

  runtime_env_secrets = concat(
    var.storage_contract.credentials_secret_name == null ? [] : [
      {
        name = var.storage_contract.credentials_secret_name
      },
    ],
    try(var.catalog_contract.floe_credentials_secret_name, null) == null ? [] : [
      {
        name = var.catalog_contract.floe_credentials_secret_name
      },
    ],
    !local.governance_enabled || try(var.governance_contract.ingestion_bot_secret_name, null) == null ? [] : [
      {
        name = var.governance_contract.ingestion_bot_secret_name
      },
    ],
  )
}
