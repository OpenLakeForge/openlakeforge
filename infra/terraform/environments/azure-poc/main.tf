terraform {
  required_version = ">= 1.7.0"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.1"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.36"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }
}

provider "kubernetes" {
  config_path    = local.kubeconfig_path
  config_context = local.kubernetes_platform_contract.kube_context
}

provider "helm" {
  repository_cache       = local.helm_repository_cache_path
  repository_config_path = local.helm_repository_config_path

  kubernetes = {
    config_path    = local.kubeconfig_path
    config_context = local.kubernetes_platform_contract.kube_context
  }
}

locals {
  kubeconfig_path = var.kubeconfig_path != null ? abspath(pathexpand(var.kubeconfig_path)) : coalesce(try(local.foundation_contract.kubeconfig_path, null), abspath("${path.root}/../../../../.tmp/kubeconfigs/azure.yaml"))
  helm_repository_cache_path = (
    var.helm_repository_cache_path != null
    ? abspath(pathexpand(var.helm_repository_cache_path))
    : abspath("${path.root}/../../../../.tmp/helm/azure/repository-cache")
  )
  helm_repository_config_path = (
    var.helm_repository_config_path != null
    ? abspath(pathexpand(var.helm_repository_config_path))
    : abspath("${path.root}/../../../../.tmp/helm/azure/repositories.yaml")
  )
  artifact_base_uri      = "s3://${var.ops_bucket_name}"
  floe_manifest_base_uri = "${local.artifact_base_uri}/floe/manifests"
  floe_report_base_uri   = "${local.artifact_base_uri}/floe/reports"
  log_base_uri           = "${local.artifact_base_uri}/logs"
  run_artifact_base_uri  = "${local.artifact_base_uri}/run-artifacts"
  domain_floe_manifest_uris = {
    sales        = "${local.floe_manifest_base_uri}/sales/sales.manifest.json"
    supply_chain = "${local.floe_manifest_base_uri}/supply_chain/supply_chain.manifest.json"
  }
  # Catalog namespaces themselves are not declared here. `olf catalog
  # sync-namespaces` reconciles them from the lakehouse inventory during
  # artifacts-deploy (ADR 0002); this root only records which naming model
  # those namespaces follow.
  catalog_namespace_model = "medallion-owner"

  # The resolved topology drives every namespace, service instance, and
  # capability gate below. Nothing here re-reads the Deployment Profile.
  enabled_stages   = { for name, stage in var.stages : name => stage if stage.enabled }
  analytics_stages = { for name, stage in local.enabled_stages : name => stage if stage.analytics }
  governed_stages  = { for name, stage in local.enabled_stages : name => stage if stage.governance }
  # Shared services are provisioned once for the whole deployment, so
  # governance follows any enabled stage asking for it.
  governance_enabled = length([for stage in values(local.enabled_stages) : true if stage.governance]) > 0

  stage_namespaces = { for name in keys(local.enabled_stages) : name => "olf-${name}" }

  # The provider contract still exports the v2 single-stage shape, so one
  # stage has to be the one a runtime consumer resolves. DEV is that stage
  # wherever it is enabled -- every promotion sources from it (ADR 0011).
  selected_stage           = contains(keys(local.enabled_stages), "dev") ? "dev" : sort(keys(local.enabled_stages))[0]
  selected_stage_namespace = local.stage_namespaces[local.selected_stage]

  # The one governed stage OpenMetadata registers until this adapter follows
  # the local one to every governed stage (#131). The selected stage need not
  # have enabled governance, so prefer it when it qualifies, else the first
  # stage that does.
  governance_dagster_stage = contains(keys(local.governed_stages), local.selected_stage) ? local.selected_stage : try(sort(keys(local.governed_stages))[0], local.selected_stage)
  stage_service_accounts   = { for name in keys(local.enabled_stages) : name => "olf-${name}-runtime" }
  stage_storage = {
    for name in keys(local.enabled_stages) : name => {
      # The Azure POC retains the same SeaweedFS-backed DEV data plane as
      # local during the v0.2-to-v3 migration. New stages remain physically
      # distinct, without relocating existing Iceberg metadata.
      bronze_bucket_name      = name == "dev" ? var.bronze_bucket_name : "${var.profile_name}-${name}-bronze",
      silver_bucket_name      = name == "dev" ? var.silver_bucket_name : "${var.profile_name}-${name}-silver",
      gold_bucket_name        = name == "dev" ? var.gold_bucket_name : "${var.profile_name}-${name}-gold",
      credentials_secret_name = "seaweedfs-${name}-s3-creds"
    }
  }
  stage_labels = { for name in keys(local.enabled_stages) : name => {
    "openlakeforge.io/stage"      = name
    "openlakeforge.io/managed-by" = "openlakeforge"
    "openlakeforge.io/profile"    = var.profile_name
  } }

  # One isolated metadata database per stage-scoped service instance, plus the
  # shared OpenMetadata one. Two Dagster instances sharing a database would
  # corrupt each other's run state; #134 completes the rest of that isolation.
  stage_databases = merge(
    {
      for name in keys(local.enabled_stages) : "dagster_${name}" => {
        key                     = "dagster_${name}"
        db_name                 = "dagster_${name}"
        db_user                 = "dagster_${name}"
        credentials_secret_name = "postgresql-dagster-${name}-creds"
        namespaces              = [local.stage_namespaces[name]]
      }
    },
    {
      for name in keys(local.analytics_stages) : "superset_${name}" => {
        key                     = "superset_${name}"
        db_name                 = "superset_${name}"
        db_user                 = "superset_${name}"
        credentials_secret_name = "postgresql-superset-${name}-creds"
        namespaces              = [local.stage_namespaces[name]]
      }
    },
    local.governance_enabled ? {
      openmetadata = {
        key                     = "openmetadata"
        db_name                 = "openmetadata_db"
        db_user                 = "openmetadata_user"
        credentials_secret_name = "postgresql-openmetadata-creds"
        namespaces              = [var.shared_namespace]
      }
    } : {},
  )
  polaris_bootstrap_hash = sha256(join("", [
    for f in sort(fileset("${path.root}/../../modules/catalog/polaris", "**/*.{tf,tftpl}")) :
    filesha256("${path.root}/../../modules/catalog/polaris/${f}")
  ]))
}

resource "kubernetes_namespace_v1" "shared" {
  metadata {
    name = var.shared_namespace
    labels = {
      "openlakeforge.io/managed-by" = "openlakeforge"
      "openlakeforge.io/profile"    = var.profile_name
      "openlakeforge.io/scope"      = "shared"
    }
  }
}

resource "kubernetes_namespace_v1" "stage" {
  for_each = local.stage_namespaces

  metadata {
    name   = each.value
    labels = local.stage_labels[each.key]
  }
}

# The identity a stage's workloads run as. Storage and catalog permissions are
# bound to it; here it exists so every stage-scoped pod already has a
# distinct, stage-labelled principal to attach them to.
resource "kubernetes_service_account_v1" "stage_runtime" {
  for_each = local.stage_service_accounts

  metadata {
    name      = each.value
    namespace = kubernetes_namespace_v1.stage[each.key].metadata[0].name
    labels    = local.stage_labels[each.key]
  }
}

module "postgresql" {
  source = "../../modules/storage/postgresql"

  namespace = kubernetes_namespace_v1.shared.metadata[0].name
  databases = values(local.stage_databases)

  depends_on = [
    kubernetes_namespace_v1.stage,
  ]
}

module "seaweedfs" {
  source = "../../modules/storage/seaweedfs"

  namespace          = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file   = "${path.root}/../../../helm/values/local/seaweedfs.yaml"
  chart_package_path = var.seaweedfs_chart_package_path
  bucket_names = concat(flatten([
    for binding in values(local.stage_storage) : [
      binding.bronze_bucket_name,
      binding.silver_bucket_name,
      binding.gold_bucket_name,
    ]
  ]), [var.ops_bucket_name])
  region = var.s3_region
  # Platform credentials stay in the shared namespace; stage workloads mount
  # only the stage-owned identities supplied through stage_credentials below.
  workload_namespaces = []
  stage_credentials = {
    for name, binding in local.stage_storage : name => {
      namespace               = local.stage_namespaces[name]
      credentials_secret_name = binding.credentials_secret_name
      bronze_bucket_name      = binding.bronze_bucket_name
      silver_bucket_name      = binding.silver_bucket_name
      gold_bucket_name        = binding.gold_bucket_name
      ops_bucket_name         = var.ops_bucket_name
    }
  }

  depends_on = [
    kubernetes_namespace_v1.stage,
  ]
}

module "polaris" {
  source = "../../modules/catalog/polaris"

  namespace             = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file      = "${path.root}/../../../helm/values/local/polaris.yaml"
  chart_package_path    = var.polaris_chart_package_path
  catalog_name          = "lakehouse_${local.selected_stage}"
  principal_name        = "trino"
  principal_role        = "data-engineer"
  catalog_role          = "catalog-admin"
  storage_contract      = local.storage_contract
  postgresql_contract   = local.metadata_database_contract
  bootstrap_revision    = local.polaris_bootstrap_hash
  workload_namespaces   = values(local.stage_namespaces)
  replicate_credentials = false
  stage_catalogs = {
    for name, binding in local.stage_storage : name => {
      namespace                        = local.stage_namespaces[name]
      catalog_name                     = "lakehouse_${name}"
      bronze_bucket_name               = binding.bronze_bucket_name
      silver_bucket_name               = binding.silver_bucket_name
      gold_bucket_name                 = binding.gold_bucket_name
      trino_principal_name             = "trino-${name}"
      trino_credentials_secret_name    = "polaris-${name}-trino-creds"
      floe_principal_name              = "floe-${name}"
      floe_credentials_secret_name     = "polaris-${name}-floe-creds"
      deployer_principal_name          = "deployer-${name}"
      deployer_credentials_secret_name = "polaris-${name}-deployer-creds"
    }
  }

  depends_on = [
    module.seaweedfs,
    kubernetes_namespace_v1.stage,
  ]
}

module "trino" {
  source = "../../modules/query/trino"

  namespace                  = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file           = "${path.root}/../../../helm/values/local/trino.yaml"
  chart_package_path         = var.trino_chart_package_path
  storage_contract           = local.storage_contract
  catalog_contract           = local.catalog_contract
  stage_catalog_contracts    = local.stage_catalog_contracts
  catalog_bootstrap_revision = local.polaris_bootstrap_hash

  depends_on = [
    module.polaris,
  ]
}

module "openmetadata" {
  source = "../../modules/governance/openmetadata"
  count  = local.governance_enabled ? 1 : 0

  namespace               = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file        = "${path.root}/../../../helm/values/local/openmetadata.yaml"
  deps_values_file        = "${path.root}/../../../helm/values/local/openmetadata-deps.yaml"
  chart_package_path      = var.openmetadata_chart_package_path
  deps_chart_package_path = var.openmetadata_deps_chart_package_path
  catalog_contract = merge(
    local.catalog_contract,
    local.stage_catalog_contracts[local.governance_dagster_stage],
  )
  storage_contract = merge(local.storage_contract, {
    bucket_name        = local.stage_storage[local.governance_dagster_stage].bronze_bucket_name
    bronze_bucket_name = local.stage_storage[local.governance_dagster_stage].bronze_bucket_name
    silver_bucket_name = local.stage_storage[local.governance_dagster_stage].silver_bucket_name
    gold_bucket_name   = local.stage_storage[local.governance_dagster_stage].gold_bucket_name
  })
  postgresql_contract = local.metadata_database_contract
  # Empty by design: the database schemas mirror Polaris namespaces, which now
  # come into existence in Phase 2. `olf openmetadata deploy-metadata` creates
  # each databaseSchema entity right before it seeds that schema's tables.
  catalog_schema_names = []
  # Only governed stages: the ingestion-bot JWT is a live credential, and a
  # stage that did not enable governance should not have one sitting in its
  # namespace even though its Dagster never mounts it.
  workload_namespaces = [for name in keys(local.governed_stages) : local.stage_namespaces[name]]
  # The complement, for stages that are still deployed but no longer governed.
  # Their namespace survives the topology change, so the ingestion-bot Secret
  # already copied there has to be deleted rather than merely not refreshed.
  revoked_namespaces = [
    for name in keys(local.enabled_stages) : local.stage_namespaces[name]
    if !contains(keys(local.governed_stages), name)
  ]
  canonical_stage = local.governance_dagster_stage
  stages = {
    for name in [local.governance_dagster_stage] : name => {
      catalog_database_name  = local.stage_catalog_contracts[name].catalog_name
      pipeline_service_name  = "dagster_${name}"
      dagster_webserver_url  = "http://${local.orchestration_contract.service_name}.${local.stage_namespaces[name]}:${local.orchestration_contract.http_port}"
      dashboard_service_name = contains(keys(local.analytics_stages), name) ? "superset_${name}" : null
      superset_url           = contains(keys(local.analytics_stages), name) ? "http://${module.superset[name].contract.service_name}.${local.stage_namespaces[name]}:${module.superset[name].contract.http_port}" : null
    }
  }
  trino_lineage_namespace = "trino://${local.query_contract.service_name}.${var.shared_namespace}:${local.query_contract.http_port}"

  depends_on = [
    module.polaris,
    module.postgresql,
    module.seaweedfs,
    kubernetes_namespace_v1.stage,
  ]
}

check "openmetadata_governance_catalog_is_unambiguous" {
  assert {
    condition     = length(local.governed_stages) <= 1
    error_message = "OpenMetadata currently has one Iceberg connection. Enable governance for one stage only; multi-stage OpenMetadata catalog connections are tracked by #131."
  }
}

module "superset" {
  source   = "../../modules/analytics/superset"
  for_each = local.analytics_stages

  namespace           = kubernetes_namespace_v1.stage[each.key].metadata[0].name
  base_values_file    = "${path.root}/../../../helm/values/local/superset.yaml"
  chart_package_path  = var.superset_chart_package_path
  image_repository    = var.superset_image_repository
  image_tag           = var.superset_image_tag
  image_pull_policy   = var.superset_image_pull_policy
  postgresql_contract = local.stage_metadata_database_contracts[each.key]

  depends_on = [
    module.postgresql,
    module.trino,
  ]
}

moved {
  from = module.openmetadata
  to   = module.openmetadata[0]
}

moved {
  from = module.superset
  to   = module.superset["dev"]
}

module "dagster" {
  source   = "../../modules/orchestration/dagster"
  for_each = local.enabled_stages

  namespace                      = kubernetes_namespace_v1.stage[each.key].metadata[0].name
  base_values_file               = "${path.root}/../../../helm/values/local/dagster.yaml"
  project_code_image_repository  = var.project_code_image_repository
  project_code_image_tag         = var.project_code_image_tag
  project_code_image_pull_policy = var.project_code_image_pull_policy
  project_code_image_revision    = var.project_code_image_revision
  manage_user_deployments        = var.manage_user_deployments
  chart_package_path             = var.dagster_chart_package_path
  storage_contract               = local.stage_storage_contracts[each.key]
  catalog_contract               = local.stage_catalog_contracts[each.key]
  # The shared governance service exists whenever any stage enables it, but a
  # stage that did not ask for governance must not receive OpenLineage
  # configuration or the ingestion-bot credential: capabilities are per stage
  # (ADR 0011).
  governance_contract = merge(local.governance_contract, {
    enabled = local.governance_enabled && each.value.governance
  })
  query_contract = merge(local.query_contract, {
    catalog_name               = local.stage_catalog_contracts[each.key].catalog_name
    runtime_identity_principal = local.stage_service_accounts[each.key]
  })
  postgresql_contract = local.stage_metadata_database_contracts[each.key]
  code_locations      = local.orchestration_contract.code_locations
  # Floe manifests are published at the ops bucket root (immutable revision,
  # not a stage activation - libs/product_dagster.py's _remote_manifest_uri,
  # olf/revision.py), not under any stage's activations/<stage> prefix.
  floe_manifest_base_uri    = local.floe_manifest_base_uri
  floe_manifest_access_mode = local.artifact_bucket_contract.access_mode
  artifact_bucket_name      = local.artifact_bucket_contract.bucket_name
  artifact_base_uri         = "${local.artifact_bucket_contract.artifact_base_uri}/activations/${each.key}"
  floe_report_base_uri      = "${local.artifact_bucket_contract.artifact_base_uri}/activations/${each.key}/floe/reports"
  log_base_uri              = "${local.artifact_bucket_contract.artifact_base_uri}/activations/${each.key}/logs"
  run_artifact_base_uri     = "${local.artifact_bucket_contract.artifact_base_uri}/activations/${each.key}/run-artifacts"

  depends_on = [
    module.trino,
    module.openmetadata,
    module.postgresql,
    module.superset,
  ]
}
