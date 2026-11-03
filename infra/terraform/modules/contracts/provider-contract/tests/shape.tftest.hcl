# The module assembles one provider contract document for three very
# different environments. Each run models one of them with plain values, so
# nothing needs a provider or credentials.

variables {
  profile_name            = "openlakeforge"
  provider_name           = "aws"
  region                  = "eu-west-1"
  environment_label       = "AWS POC"
  shared_namespace        = "olf-system"
  stage_namespaces        = { dev = "olf-dev", prod = "olf-prod" }
  kube_context            = "ctx"
  foundation_kube_context = "ctx"
  implementations = {
    foundation          = "foundation.eks"
    kubernetes_platform = "kubernetes.eks"
    metadata_database   = "metadata_database.aws_rds_postgresql"
    secrets             = "secrets.kubernetes_secret_on_eks"
    observability       = "observability.object_log_archive_on_eks"
    artifact_registry   = "artifacts.aws_ecr"
  }
  query = { implementation = "query.trino_on_eks", endpoint = "http://trino.olf-system:8080" }
  catalog = {
    catalog_type         = "glue"
    trino_support        = ["glue"]
    dagster_support      = ["glue"]
    floe_support         = ["glue"]
    dbt_support          = ["glue"]
    openmetadata_support = ["glue"]
  }
  ops_storage = {
    implementation           = "artifacts.aws_s3_bucket"
    bucket_name              = "ops"
    artifact_base_uri        = "s3://ops/artifacts"
    access_mode              = "remote"
    local_upload_access_mode = "direct"
    credentials_secret_name  = null
    access_key_id_key        = null
    secret_access_key_key    = null
  }
  identity       = { implementation = "identity.aws_pod_identity", roles = { precedence = ["admin"] } }
  access         = { implementation = "access.kubectl_port_forward" }
  code_locations = [{ name = "openlakeforge-dagster", definitions_module = "lakehouse_code.definitions" }]
  metadata_database = {
    host = "db"
    port = 5432
  }
  stage_databases = {
    dagster_dev  = { db_name = "dagster_dev", db_user = "dagster_dev", credentials_secret_name = "pg-dagster-dev" }
    superset_dev = { db_name = "superset_dev", db_user = "superset_dev", credentials_secret_name = "pg-superset-dev" }
    dagster_prod = { db_name = "dagster_prod", db_user = "dagster_prod", credentials_secret_name = "pg-dagster-prod" }
    openmetadata = { db_name = "openmetadata", db_user = "openmetadata", credentials_secret_name = "pg-om" }
  }
  stage_service_namespaces = [
    { stage = "dev", namespace = "olf-dev" },
    { stage = "prod", namespace = "olf-prod" },
  ]
  stages = {
    dev = {
      namespace            = "olf-dev"
      analytics            = true
      governance           = true
      service_account      = "olf-dev-runtime"
      catalog_endpoint_ref = "stage/dev/catalog"
      query_catalog_name   = "lakehouse_dev"
      storage              = { implementation = "storage.aws_s3" }
      catalog              = { catalog_type = "glue" }
    }
    prod = {
      namespace            = "olf-prod"
      analytics            = false
      governance           = false
      service_account      = "olf-prod-runtime"
      catalog_endpoint_ref = "stage/prod/catalog"
      query_catalog_name   = "lakehouse_prod"
      storage              = { implementation = "storage.aws_s3" }
      catalog              = { catalog_type = "glue" }
    }
  }
}

run "aws_has_no_shared_catalog_portal_or_governance_service" {
  command = plan

  assert {
    condition     = toset(keys(output.provider_contracts.shared)) == toset(["foundation", "kubernetes_platform", "metadata_database", "query", "artifact_registry", "ops_storage", "secrets", "identity", "access", "observability"])
    error_message = "without catalog_service, portal or governance_service the shared block holds exactly the ten always-present bindings"
  }

  assert {
    condition     = output.provider_contracts.deployment.region == "eu-west-1" && output.provider_contracts.schema_version == "3.0.0"
    error_message = "the deployment block carries the provider region and the v3 schema version"
  }

  assert {
    condition     = output.provider_contracts.stages.dev.endpoints.catalog == "stage/dev/catalog" && output.provider_contracts.shared.access == { ref = "shared/access", implementation = "access.kubectl_port_forward" }
    error_message = "a stage-scoped catalog is referenced by its stage ref, and an empty access extra adds nothing"
  }

  assert {
    condition     = !contains(keys(output.provider_contracts.stages.dev.orchestration), "pipeline_service_name") && !contains(keys(output.provider_contracts.stages.dev.reporting), "dashboard_service_name")
    error_message = "service names are absent unless the environment supplies them"
  }
}

run "local_adds_shared_services_and_stage_extras" {
  command = plan

  variables {
    provider_name         = "local"
    region                = null
    catalog_service       = { implementation = "catalog.iceberg_rest.polaris", endpoint = "http://polaris:8181/api/catalog" }
    portal_implementation = "portal.static_page"
    governance_service    = { implementation = "governance.openmetadata", endpoint = "http://om.olf-system:8585" }
    identity = {
      implementation = "identity.oidc"
      roles          = { precedence = ["admin"] }
      extra          = { issuer_url = "https://auth.example" }
    }
    access = {
      implementation = "access.ingress"
      extra          = { base_domain = "example.localhost", tls_mode = "none" }
    }
    stages = {
      dev = {
        namespace              = "olf-dev"
        analytics              = true
        governance             = true
        service_account        = "olf-dev-runtime"
        catalog_endpoint_ref   = "shared/catalog_service"
        query_catalog_name     = "lakehouse_dev"
        query_extra            = { tls_port = "8443" }
        pipeline_service_name  = "dagster_dev"
        dashboard_service_name = "superset_dev"
        storage                = { implementation = "storage.s3_compatible.seaweedfs" }
        catalog                = { catalog_type = "rest" }
      }
    }
    stage_namespaces         = { dev = "olf-dev" }
    stage_service_namespaces = [{ stage = "dev", namespace = "olf-dev" }]
  }

  assert {
    condition     = output.provider_contracts.deployment.region == null
    error_message = "a provider without a region reports null"
  }

  assert {
    condition     = toset(["catalog_service", "portal", "governance_service"]) == setintersection(toset(keys(output.provider_contracts.shared)), toset(["catalog_service", "portal", "governance_service"]))
    error_message = "a supplied catalog service, portal and governance service each add their shared binding"
  }

  assert {
    condition     = output.provider_contracts.shared.identity.issuer_url == "https://auth.example" && output.provider_contracts.shared.access.base_domain == "example.localhost"
    error_message = "identity and access extras are merged after ref and implementation"
  }

  assert {
    condition     = output.provider_contracts.stages.dev.query.tls_port == "8443" && output.provider_contracts.stages.dev.orchestration.pipeline_service_name == "dagster_dev" && output.provider_contracts.stages.dev.reporting.dashboard_service_name == "superset_dev"
    error_message = "query extras and service names reach the stage blocks"
  }

  assert {
    condition     = output.provider_contracts.stages.dev.endpoints.catalog == "shared/catalog_service"
    error_message = "a shared catalog is referenced through the shared service"
  }
}

run "capabilities_follow_the_stage_flags" {
  command = plan

  assert {
    condition     = contains(keys(output.provider_contracts.stages.dev), "reporting") && contains(keys(output.provider_contracts.stages.dev), "governance") && !contains(keys(output.provider_contracts.stages.prod), "reporting") && !contains(keys(output.provider_contracts.stages.prod), "governance")
    error_message = "reporting and governance blocks exist only for stages that enable them"
  }

  assert {
    condition     = contains(keys(output.stage_metadata_database_contracts.dev), "superset_db_name") && !contains(keys(output.stage_metadata_database_contracts.prod), "superset_db_name")
    error_message = "only analytics stages get a Superset database"
  }

  assert {
    condition     = output.stage_metadata_database_contracts.prod.dagster_db_name == "dagster_prod" && output.stage_metadata_database_contracts.prod.host == "db"
    error_message = "each stage keeps the shared database contract and its own Dagster database"
  }
}
