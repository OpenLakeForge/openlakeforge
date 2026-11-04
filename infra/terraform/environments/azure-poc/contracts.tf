data "terraform_remote_state" "azure_foundation" {
  backend = "local"

  config = {
    path = local.foundation_state_path
  }
}

locals {
  azure_provider_name   = "azure"
  foundation_state_path = var.foundation_state_path != null ? abspath(pathexpand(var.foundation_state_path)) : abspath("${path.root}/../../foundations/azure-aks/terraform.tfstate")

  foundation_contract = merge(data.terraform_remote_state.azure_foundation.outputs.foundation_contract, {
    provider              = local.azure_provider_name
    implementation        = "foundation.aks"
    adapter               = "foundation.aks"
    cluster_type          = "aks"
    network_model         = "aks-kubenet"
    platform_state_model  = "separate-terraform-root"
    platform_apply_order  = "foundation-before-platform"
    supported_environment = "azure-poc"
  })

  kubernetes_platform_contract = {
    provider       = local.azure_provider_name
    implementation = "kubernetes.aks"
    adapter        = "platform.kubernetes.aks"
    # The stage a runtime consumer resolves. Shared services live in
    # `shared_namespace`.
    namespace            = local.selected_stage_namespace
    shared_namespace     = var.shared_namespace
    stage                = local.selected_stage
    stage_namespaces     = local.stage_namespaces
    kube_context         = coalesce(try(local.foundation_contract.kube_context, null), var.kube_context)
    kubeconfig_path      = coalesce(try(local.foundation_contract.kubeconfig_path, null), local.kubeconfig_path)
    cluster_name         = try(local.foundation_contract.cluster_name, "aks-openlakeforge-poc")
    resource_group_name  = try(local.foundation_contract.resource_group_name, null)
    location             = try(local.foundation_contract.location, null)
    platform_apply_model = "foundation-state-kube-context"
    workload_identity    = "azure-workload-identity-ready"
  }

  stage_storage_contracts = {
    for name, binding in local.stage_storage : name => merge(module.seaweedfs.contract, {
      provider                = local.azure_provider_name
      implementation          = "storage.s3_compatible.seaweedfs_on_aks"
      adapter                 = "storage.s3_compatible.seaweedfs_on_aks"
      logical_name            = "stage/${name}/storage"
      protocol                = "s3"
      auth_mode               = "static-access-key-secret"
      secret_delivery_mode    = "kubernetes-secret-env"
      workload_identity       = false
      ssl_mode                = "disabled"
      ingress_mode            = "cluster-internal"
      local_only              = false
      poc_only                = true
      future_adapter_shapes   = ["storage.azure_blob_or_adls_gen2"]
      bronze_bucket_name      = binding.bronze_bucket_name
      silver_bucket_name      = binding.silver_bucket_name
      gold_bucket_name        = binding.gold_bucket_name
      credentials_secret_name = module.seaweedfs.stage_credentials[name].credentials_secret_name
      access_key_id_key       = module.seaweedfs.stage_credentials[name].access_key_id_key
      secret_access_key_key   = module.seaweedfs.stage_credentials[name].secret_access_key_key
    })
  }

  # Shared platform services keep their own administrative S3 binding. Runtime
  # workloads below receive only the corresponding stage_storage_contract.
  storage_contract = merge(module.seaweedfs.contract, {
    provider              = local.azure_provider_name
    implementation        = "storage.s3_compatible.seaweedfs_on_aks"
    adapter               = "storage.s3_compatible.seaweedfs_on_aks"
    logical_name          = "lakehouse_storage"
    protocol              = "s3"
    auth_mode             = "static-access-key-secret"
    secret_delivery_mode  = "kubernetes-secret-env"
    workload_identity     = false
    ssl_mode              = "disabled"
    ingress_mode          = "cluster-internal"
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["storage.azure_blob_or_adls_gen2"]
    bronze_bucket_name    = local.stage_storage[local.selected_stage].bronze_bucket_name
    silver_bucket_name    = local.stage_storage[local.selected_stage].silver_bucket_name
    gold_bucket_name      = local.stage_storage[local.selected_stage].gold_bucket_name
  })

  # One contract per stage-scoped service instance: the shared PostgreSQL
  # server with that stage's own database, user, and credentials Secret.
  stage_metadata_database_contracts = module.provider_contract.stage_metadata_database_contracts

  # The shared OpenMetadata database, flattened onto the contract the
  # governance module's typed input expects. Stage-scoped services take their
  # own database through `stage_metadata_database_contracts` instead.
  governance_database = local.governance_enabled ? local.stage_databases["openmetadata"] : null

  metadata_database_contract = merge(module.postgresql.contract, {
    openmetadata_db_name                 = try(local.governance_database.db_name, null)
    openmetadata_db_user                 = try(local.governance_database.db_user, null)
    openmetadata_credentials_secret_name = try(local.governance_database.credentials_secret_name, null)

    provider              = local.azure_provider_name
    implementation        = "metadata_database.postgresql.in_cluster_on_aks"
    adapter               = "metadata_database.postgresql.in_cluster_on_aks"
    engine                = "postgresql"
    logical_name          = "platform_metadata"
    auth_mode             = "static-password-secret"
    secret_delivery_mode  = "kubernetes-secret-env"
    ssl_mode              = "disabled"
    endpoint              = "${module.postgresql.contract.host}:${module.postgresql.contract.port}"
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["metadata_database.azure_postgresql_flexible_server"]
  })

  catalog_contract = merge(module.polaris.contract, {
    provider                   = local.azure_provider_name
    implementation             = "catalog.iceberg_rest.polaris_on_aks"
    adapter                    = "catalog.iceberg_rest.polaris_on_aks"
    logical_name               = "iceberg_catalog"
    catalog_provider           = "polaris"
    catalog_type               = "rest"
    catalog_name               = "lakehouse_${local.selected_stage}"
    runtime_profile            = "polaris-rest"
    trino_catalog_name         = "iceberg"
    default_warehouse_location = "s3://${local.stage_storage[local.selected_stage].silver_bucket_name}"
    catalog_namespace_model    = local.catalog_namespace_model
    auth_mode                  = "oauth-client-secret"
    secret_delivery_mode       = "kubernetes-secret-env"
    ssl_mode                   = "disabled"
    endpoint                   = module.polaris.contract.rest_uri
    ingress_mode               = "cluster-internal"
    local_only                 = false
    poc_only                   = true
    implemented_catalog_types  = ["rest"]
    future_catalog_types       = ["rest"]
    future_adapter_shapes      = ["catalog.polaris_with_azure_storage"]
    trino_support              = ["rest"]
    dagster_support            = ["rest"]
    floe_support               = ["rest"]
    dbt_support                = ["rest"]
    openmetadata_support       = ["rest"]
    catalog_database_fqn       = "polaris.lakehouse_${local.selected_stage}"
    # Per-product namespaces and schema FQNs are deliberately absent: Phase 2
    # reconciles them from the descriptors (ADR 0002), and olf/infra/contracts.py
    # derives both from the same inventory when the contract omits them.
  })

  stage_catalog_contracts = {
    for name, binding in module.polaris.stage_contracts : name => merge(binding, {
      provider                  = local.azure_provider_name
      implementation            = "catalog.iceberg_rest.polaris_on_aks"
      adapter                   = "catalog.iceberg_rest.polaris_on_aks"
      logical_name              = "stage/${name}/catalog"
      catalog_provider          = "polaris"
      catalog_type              = "rest"
      runtime_profile           = "polaris-rest"
      catalog_namespace_model   = local.catalog_namespace_model
      auth_mode                 = "oauth-client-secret"
      secret_delivery_mode      = "kubernetes-secret-env"
      ssl_mode                  = "disabled"
      endpoint                  = module.polaris.contract.rest_uri
      ingress_mode              = "cluster-internal"
      local_only                = false
      poc_only                  = true
      implemented_catalog_types = ["rest"]
      future_catalog_types      = ["rest"]
      future_adapter_shapes     = ["catalog.polaris_with_azure_storage"]
      trino_support             = ["rest"]
      dagster_support           = ["rest"]
      floe_support              = ["rest"]
      dbt_support               = ["rest"]
      openmetadata_support      = ["rest"]
      catalog_database_fqn      = "polaris.${binding.catalog_name}"
    })
  }

  governance_contract = merge(local.governance_enabled ? module.openmetadata[0].contract : {}, {
    enabled        = local.governance_enabled
    provider       = local.azure_provider_name
    implementation = "governance.openmetadata_on_aks"
    adapter        = "governance.openmetadata_on_aks"
    logical_name   = "governance_catalog"
    auth_mode      = "local-development"
    endpoint       = local.governance_enabled ? "http://${module.openmetadata[0].contract.service_name}.${module.openmetadata[0].contract.service_namespace}:${module.openmetadata[0].contract.http_port}" : null
    ingress_mode   = "cluster-internal"
    local_only     = false
    poc_only       = true
  })

  selected_stage_analytics = contains(keys(local.analytics_stages), local.selected_stage)

  # `try`, not a conditional: indexing a for_each module with a key it does
  # not have is an error even on the unselected branch of a ternary.
  reporting_contract = merge(try(module.superset[local.selected_stage].contract, {}), {
    enabled        = local.selected_stage_analytics
    provider       = local.azure_provider_name
    implementation = "reporting.superset_on_aks"
    adapter        = "reporting.superset_on_aks"
    logical_name   = "bi_reporting"
    auth_mode      = "local-development"
    endpoint = try(
      "http://${module.superset[local.selected_stage].contract.service_name}.${local.selected_stage_namespace}:${module.superset[local.selected_stage].contract.http_port}",
      null,
    )
    ingress_mode = "cluster-internal"
    local_only   = false
    poc_only     = true
  })

  query_contract = {
    provider          = local.azure_provider_name
    implementation    = "query.trino_on_aks"
    adapter           = "query.trino_on_aks"
    logical_name      = "sql_query"
    service_name      = "trino"
    service_namespace = var.shared_namespace
    http_port         = 8080
    # Namespace-qualified: stage-scoped Dagster and Superset resolve this
    # from their own namespace, where a bare service name would not resolve.
    endpoint            = "http://trino.${var.shared_namespace}:8080"
    catalog_name        = local.catalog_contract.trino_catalog_name
    supported_catalogs  = ["rest"]
    active_catalog_type = local.catalog_contract.catalog_type
    storage_ref         = local.storage_contract.logical_name
    catalog_ref         = local.catalog_contract.logical_name
    ingress_mode        = "cluster-internal"
    future_adapter_shapes = [
      "query.trino",
    ]
  }

  orchestration_contract = {
    provider          = local.azure_provider_name
    implementation    = "orchestration.dagster_on_aks"
    adapter           = "orchestration.dagster_on_aks"
    logical_name      = "orchestration"
    service_name      = "dagster-dagster-webserver"
    service_namespace = local.selected_stage_namespace
    http_port         = 80
    endpoint          = "http://dagster-dagster-webserver.${local.selected_stage_namespace}:80"
    code_locations = [
      {
        name               = "openlakeforge-dagster"
        definitions_module = "lakehouse_code.definitions"
      },
    ]
    runner                    = "kubernetes-run-launcher"
    project_code_image        = "${var.project_code_image_repository}:${var.project_code_image_tag}"
    project_code_image_policy = var.project_code_image_pull_policy
    log_base_uri              = local.log_base_uri
    run_artifact_base_uri     = local.run_artifact_base_uri
    supported_catalogs        = ["rest"]
    active_catalog_type       = local.catalog_contract.catalog_type
    storage_ref               = local.storage_contract.logical_name
    catalog_ref               = local.catalog_contract.logical_name
    artifact_bucket_ref       = "ops_artifacts"
    local_only                = false
    poc_only                  = true
    future_adapter_shapes     = ["orchestration.dagster"]
  }

  artifact_registry_contract = {
    provider                  = local.azure_provider_name
    implementation            = "artifacts.azure_acr"
    adapter                   = "artifacts.azure_acr"
    logical_name              = "runtime_images"
    acr_login_server          = try(local.foundation_contract.acr_login_server, null)
    project_code_image        = "${var.project_code_image_repository}:${var.project_code_image_tag}"
    project_code_image_policy = var.project_code_image_pull_policy
    distribution_mode         = "registry-push"
    target_cluster            = local.kubernetes_platform_contract.cluster_name
    local_only                = false
    poc_only                  = true
    future_adapter_shapes     = ["artifacts.azure_acr", "artifacts.github_actions_to_acr"]
  }

  artifact_bucket_contract = {
    provider                 = local.azure_provider_name
    implementation           = "artifacts.s3_compatible_bucket.seaweedfs_on_aks"
    adapter                  = "artifacts.s3_compatible_bucket.seaweedfs_on_aks"
    logical_name             = "ops_artifacts"
    bucket_name              = var.ops_bucket_name
    artifact_base_uri        = local.artifact_base_uri
    access_mode              = "remote"
    log_base_uri             = local.log_base_uri
    run_artifact_base_uri    = local.run_artifact_base_uri
    distribution_mode        = "s3-compatible-upload"
    storage_ref              = local.storage_contract.logical_name
    credentials_secret_name  = local.storage_contract.credentials_secret_name
    access_key_id_key        = local.storage_contract.access_key_id_key
    secret_access_key_key    = local.storage_contract.secret_access_key_key
    local_upload_access_mode = "kubectl-port-forward"
    local_only               = false
    poc_only                 = true
    future_adapter_shapes    = ["artifacts.azure_blob_or_adls_gen2"]
  }

  artifact_contract = merge(local.artifact_registry_contract, {
    implementation        = "artifacts.azure_acr_and_s3_compatible_bucket"
    adapter               = "artifacts.azure_acr_and_s3_compatible_bucket"
    ops_bucket_name       = local.artifact_bucket_contract.bucket_name
    artifact_base_uri     = local.artifact_bucket_contract.artifact_base_uri
    log_base_uri          = local.artifact_bucket_contract.log_base_uri
    run_artifact_base_uri = local.artifact_bucket_contract.run_artifact_base_uri
  })

  secrets_contract = {
    provider              = local.azure_provider_name
    implementation        = "secrets.kubernetes_secret_on_aks"
    adapter               = "secrets.kubernetes_secret_on_aks"
    backend               = "kubernetes"
    delivery_mode         = "env-from-secret"
    rotation_mode         = "manual-poc"
    references_only       = true
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["secrets.azure_key_vault_external_secrets"]
  }

  # ADR 0014: the product-level role model, identical on every provider.
  identity_roles = yamldecode(file("${path.root}/../../../../release/identity-roles.yaml"))

  identity_contract = {
    provider              = local.azure_provider_name
    implementation        = "identity.azure_workload_identity_ready"
    adapter               = "identity.azure_workload_identity_ready"
    auth_mode             = "basic-poc"
    oidc_enabled          = true
    oidc_issuer_url       = try(local.foundation_contract.oidc_issuer_url, null)
    workload_identity     = "aks-workload-identity-enabled"
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["identity.azure_workload_identity", "identity.managed_identity"]
  }

  access_contract = {
    provider              = local.azure_provider_name
    implementation        = "access.kubectl_port_forward"
    adapter               = "access.kubectl_port_forward"
    ingress_mode          = "port-forward"
    internal_access_mode  = "cluster-dns"
    external_access_mode  = "localhost-port-forward"
    tls_mode              = "none-poc"
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["access.ingress", "access.load_balancer", "access.private_dns"]
  }

  observability_contract = {
    provider              = local.azure_provider_name
    implementation        = "observability.object_log_archive_on_aks"
    adapter               = "observability.object_log_archive_on_aks"
    metrics_enabled       = false
    tracing_enabled       = false
    logs_mode             = "s3-compatible-object-archive"
    log_base_uri          = local.log_base_uri
    compute_log_uri       = "${local.log_base_uri}/dagster/compute"
    kubernetes_log_uri    = "${local.log_base_uri}/k8s"
    artifact_bucket_ref   = local.artifact_bucket_contract.logical_name
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["observability.loki_grafana", "observability.azure_monitor", "observability.managed_prometheus"]
  }

  provider_contracts = module.provider_contract.provider_contracts
}

# The contract document, the per-stage metadata database contracts and the
# cross-field checks come from the shared module; this root supplies only what
# is specific to the Azure provider.
module "provider_contract" {
  source = "../../modules/contracts/provider-contract"

  profile_name            = var.profile_name
  provider_name           = local.azure_provider_name
  environment_label       = "Azure POC"
  shared_namespace        = var.shared_namespace
  stage_namespaces        = local.stage_namespaces
  kube_context            = local.kubernetes_platform_contract.kube_context
  foundation_kube_context = local.foundation_contract.kube_context

  implementations = {
    foundation          = local.foundation_contract.implementation
    kubernetes_platform = local.kubernetes_platform_contract.implementation
    metadata_database   = local.metadata_database_contract.implementation
    secrets             = local.secrets_contract.implementation
    observability       = local.observability_contract.implementation
    artifact_registry   = local.artifact_registry_contract.implementation
  }

  query           = local.query_contract
  catalog_service = local.catalog_contract
  catalog         = local.catalog_contract
  ops_storage     = local.artifact_bucket_contract

  identity = {
    implementation = local.identity_contract.implementation
    roles          = local.identity_roles
  }

  access = {
    implementation = local.access_contract.implementation
  }

  # OpenMetadata is one shared instance across every governed stage, so its
  # binding is shared rather than duplicated per stage.
  governance_service = local.governance_enabled ? local.governance_contract : null

  # The same list the Dagster module renders workspace.yaml from, and the set
  # a stage's user deployments are rendered from.
  code_locations = local.orchestration_contract.code_locations

  metadata_database = local.metadata_database_contract
  stage_databases   = local.stage_databases

  stage_service_namespaces = concat(
    [for name, instance in module.dagster : { stage = name, namespace = instance.namespace }],
    [for name, instance in module.superset : { stage = name, namespace = instance.namespace }],
  )

  stages = {
    for name, stage in local.enabled_stages : name => {
      namespace            = local.stage_namespaces[name]
      analytics            = stage.analytics
      governance           = stage.governance
      service_account      = local.stage_service_accounts[name]
      catalog_endpoint_ref = "shared/catalog_service"
      query_catalog_name   = local.stage_catalog_contracts[name].catalog_name
      storage = {
        provider                = local.azure_provider_name
        implementation          = "storage.s3_compatible.seaweedfs_on_aks"
        protocol                = "s3"
        region                  = var.s3_region
        endpoint                = local.stage_storage_contracts[name].endpoint
        virtual_host_endpoint   = local.stage_storage_contracts[name].virtual_host_endpoint
        path_style_access       = true
        ssl_mode                = "disabled"
        credentials_secret_name = local.stage_storage_contracts[name].credentials_secret_name
        access_key_id_key       = local.stage_storage_contracts[name].access_key_id_key
        secret_access_key_key   = local.stage_storage_contracts[name].secret_access_key_key
        s3_service_name         = local.stage_storage_contracts[name].s3_service_name
        s3_service_port         = local.stage_storage_contracts[name].s3_service_port
        identity_ref            = "stage/${name}/runtime_identity"
        bronze = {
          physical_id = local.stage_storage[name].bronze_bucket_name
          bucket_name = local.stage_storage[name].bronze_bucket_name
          uri         = "s3://${local.stage_storage[name].bronze_bucket_name}"
        }
        silver = {
          physical_id = local.stage_storage[name].silver_bucket_name
          bucket_name = local.stage_storage[name].silver_bucket_name
          uri         = "s3://${local.stage_storage[name].silver_bucket_name}"
        }
        gold = {
          physical_id = local.stage_storage[name].gold_bucket_name
          bucket_name = local.stage_storage[name].gold_bucket_name
          uri         = "s3://${local.stage_storage[name].gold_bucket_name}"
        }
      }
      catalog = {
        logical_name                     = "iceberg_catalog"
        implementation                   = local.stage_catalog_contracts[name].implementation
        catalog_type                     = "rest"
        catalog_provider                 = "polaris"
        catalog_name                     = local.stage_catalog_contracts[name].catalog_name
        runtime_profile                  = "polaris-rest"
        physical_id                      = local.stage_catalog_contracts[name].catalog_name
        service_ref                      = "shared/catalog_service"
        warehouse                        = local.stage_catalog_contracts[name].warehouse
        rest_uri                         = local.stage_catalog_contracts[name].rest_uri
        token_uri                        = local.stage_catalog_contracts[name].token_uri
        oauth_scope                      = local.stage_catalog_contracts[name].oauth_scope
        catalog_namespace_model          = local.catalog_namespace_model
        floe_credentials_secret_name     = local.stage_catalog_contracts[name].floe_credentials_secret_name
        floe_client_id_key               = local.stage_catalog_contracts[name].floe_client_id_key
        floe_client_secret_key           = local.stage_catalog_contracts[name].floe_client_secret_key
        deployer_credentials_secret_name = local.stage_catalog_contracts[name].deployer_credentials_secret_name
        deployer_client_id_key           = local.stage_catalog_contracts[name].deployer_client_id_key
        deployer_client_secret_key       = local.stage_catalog_contracts[name].deployer_client_secret_key
      }
    }
  }
}

check "azure_contract_adapters_are_explicit" {
  assert {
    condition = alltrue([
      local.foundation_contract.provider == local.azure_provider_name && local.foundation_contract.implementation != "" && local.foundation_contract.adapter != "",
      local.kubernetes_platform_contract.provider == local.azure_provider_name && local.kubernetes_platform_contract.implementation != "" && local.kubernetes_platform_contract.adapter != "",
      local.storage_contract.provider == local.azure_provider_name && local.storage_contract.implementation != "" && local.storage_contract.adapter != "",
      local.metadata_database_contract.provider == local.azure_provider_name && local.metadata_database_contract.implementation != "" && local.metadata_database_contract.adapter != "",
      local.catalog_contract.provider == local.azure_provider_name && local.catalog_contract.implementation != "" && local.catalog_contract.adapter != "",
      local.query_contract.provider == local.azure_provider_name && local.query_contract.implementation != "" && local.query_contract.adapter != "",
      local.orchestration_contract.provider == local.azure_provider_name && local.orchestration_contract.implementation != "" && local.orchestration_contract.adapter != "",
      local.governance_contract.provider == local.azure_provider_name && local.governance_contract.implementation != "" && local.governance_contract.adapter != "",
      local.reporting_contract.provider == local.azure_provider_name && local.reporting_contract.implementation != "" && local.reporting_contract.adapter != "",
      local.artifact_registry_contract.provider == local.azure_provider_name && local.artifact_registry_contract.implementation != "" && local.artifact_registry_contract.adapter != "",
      local.artifact_bucket_contract.provider == local.azure_provider_name && local.artifact_bucket_contract.implementation != "" && local.artifact_bucket_contract.adapter != "",
      local.secrets_contract.provider == local.azure_provider_name && local.secrets_contract.implementation != "" && local.secrets_contract.adapter != "",
      local.identity_contract.provider == local.azure_provider_name && local.identity_contract.implementation != "" && local.identity_contract.adapter != "",
      local.access_contract.provider == local.azure_provider_name && local.access_contract.implementation != "" && local.access_contract.adapter != "",
      local.observability_contract.provider == local.azure_provider_name && local.observability_contract.implementation != "" && local.observability_contract.adapter != "",
    ])
    error_message = "Every Azure POC provider contract must declare provider, implementation, and adapter."
  }
}

check "azure_poc_keeps_s3_compatible_storage" {
  assert {
    condition = alltrue([
      local.storage_contract.protocol == "s3",
      local.storage_contract.implementation == "storage.s3_compatible.seaweedfs_on_aks",
      local.artifact_bucket_contract.local_upload_access_mode == "kubectl-port-forward",
    ])
    error_message = "The first Azure POC must keep SeaweedFS S3-compatible storage until Azure Blob/ADLS adapters are implemented."
  }
}

check "azure_poc_uses_acr_artifacts" {
  assert {
    condition = alltrue([
      local.artifact_registry_contract.implementation == "artifacts.azure_acr",
      local.artifact_registry_contract.distribution_mode == "registry-push",
      var.project_code_image_pull_policy == "Always",
    ])
    error_message = "The Azure POC must distribute runtime images through ACR with imagePullPolicy=Always."
  }
}

check "openmetadata_catalog_fqn_uses_lakehouse_database" {
  assert {
    condition     = local.catalog_contract.catalog_database_fqn == "polaris.lakehouse_${local.selected_stage}" && local.catalog_contract.catalog_name != "default"
    error_message = "OpenMetadata catalog assets must resolve under polaris.<catalog_name>, not polaris.default."
  }
}
