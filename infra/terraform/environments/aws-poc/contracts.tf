data "terraform_remote_state" "aws_foundation" {
  backend = "local"

  config = {
    path = local.foundation_state_path
  }
}

locals {
  aws_provider_name     = "aws"
  foundation_state_path = var.foundation_state_path != null ? abspath(pathexpand(var.foundation_state_path)) : abspath("${path.root}/../../foundations/aws-eks/terraform.tfstate")

  foundation_contract = merge(data.terraform_remote_state.aws_foundation.outputs.foundation_contract, {
    provider              = local.aws_provider_name
    implementation        = "foundation.eks"
    adapter               = "foundation.eks"
    cluster_type          = "eks"
    network_model         = "aws-vpc-cni"
    platform_state_model  = "separate-terraform-root"
    platform_apply_order  = "foundation-before-platform"
    supported_environment = "aws-poc"
  })

  kubernetes_platform_contract = {
    provider       = local.aws_provider_name
    implementation = "kubernetes.eks"
    adapter        = "platform.kubernetes.eks"
    # The stage a runtime consumer resolves. Shared services live in
    # `shared_namespace`.
    namespace            = local.selected_stage_namespace
    shared_namespace     = var.shared_namespace
    stage                = local.selected_stage
    stage_namespaces     = local.stage_namespaces
    kube_context         = coalesce(try(local.foundation_contract.kube_context, null), var.kube_context)
    kubeconfig_path      = coalesce(try(local.foundation_contract.kubeconfig_path, null), local.kubeconfig_path)
    cluster_name         = try(local.foundation_contract.cluster_name, "eks-openlakeforge-poc")
    aws_region           = local.aws_region
    platform_apply_model = "foundation-state-kube-context"
    workload_identity    = "aws-pod-identity"
  }

  stage_storage_contracts = {
    for name, binding in module.s3.stage_contracts : name => merge(binding, {
      provider       = local.aws_provider_name
      implementation = "storage.aws_s3"
      adapter        = "storage.aws_s3"
      logical_name   = "stage/${name}/storage"
      protocol       = "s3"
      region         = local.aws_region
      # Legacy single-bucket alias every storage_contract consumer's
      # variable schema still requires (see local/contracts.tf's own
      # seaweedfs.contract, whose `bucket_name` is likewise its bronze
      # bucket by convention). Bronze/Silver/Gold are the real inputs.
      bucket_name          = binding.bronze_bucket_name
      auth_mode            = "aws-pod-identity"
      secret_delivery_mode = "none"
      workload_identity    = true
      ssl_mode             = "required"
      ingress_mode         = "aws-service-endpoint"
      local_only           = false
      poc_only             = true
    })
  }

  # Shared platform services (Trino, OpenMetadata) authenticate via their own
  # broad IAM role, not a stage's. Runtime workloads below receive only the
  # corresponding stage_storage_contract.
  storage_contract = merge(module.s3.stage_contracts[local.selected_stage], {
    provider             = local.aws_provider_name
    implementation       = "storage.aws_s3"
    adapter              = "storage.aws_s3"
    logical_name         = "lakehouse_storage"
    protocol             = "s3"
    region               = local.aws_region
    bucket_name          = module.s3.stage_contracts[local.selected_stage].bronze_bucket_name
    auth_mode            = "aws-pod-identity"
    secret_delivery_mode = "none"
    workload_identity    = true
    ssl_mode             = "required"
    ingress_mode         = "aws-service-endpoint"
    local_only           = false
    poc_only             = true
    ops_bucket_name      = module.s3.ops_bucket_name
  })

  stage_metadata_database_contracts = { for name in keys(local.enabled_stages) : name => merge(
    local.metadata_database_contract,
    {
      dagster_db_name                 = local.stage_databases["dagster_${name}"].db_name
      dagster_db_user                 = local.stage_databases["dagster_${name}"].db_user
      dagster_credentials_secret_name = local.stage_databases["dagster_${name}"].credentials_secret_name
    },
    contains(keys(local.analytics_stages), name) ? {
      superset_db_name                 = local.stage_databases["superset_${name}"].db_name
      superset_db_user                 = local.stage_databases["superset_${name}"].db_user
      superset_credentials_secret_name = local.stage_databases["superset_${name}"].credentials_secret_name
    } : {},
  ) }

  governance_database = local.governance_enabled ? local.stage_databases["openmetadata"] : null

  metadata_database_contract = merge(module.rds_postgresql.contract, {
    openmetadata_db_name                 = try(local.governance_database.db_name, null)
    openmetadata_db_user                 = try(local.governance_database.db_user, null)
    openmetadata_credentials_secret_name = try(local.governance_database.credentials_secret_name, null)

    provider             = local.aws_provider_name
    implementation       = "metadata_database.aws_rds_postgresql"
    adapter              = "metadata_database.aws_rds_postgresql"
    engine               = "postgresql"
    logical_name         = "platform_metadata"
    auth_mode            = "static-password-secret"
    secret_delivery_mode = "kubernetes-secret-env"
    ssl_mode             = "required"
    endpoint             = "${module.rds_postgresql.contract.host}:${module.rds_postgresql.contract.port}"
    local_only           = false
    poc_only             = true
  })

  catalog_contract = merge(module.glue.stage_contracts[local.selected_stage], {
    provider                   = local.aws_provider_name
    implementation             = "catalog.aws_glue"
    adapter                    = "catalog.aws_glue"
    logical_name               = "iceberg_catalog"
    catalog_provider           = "aws-glue"
    catalog_type               = "glue"
    default_warehouse_location = "s3://${local.stage_storage_contracts[local.selected_stage].silver_bucket_name}"
    catalog_namespace_model    = local.catalog_namespace_model
    auth_mode                  = "aws-sigv4-pod-identity"
    secret_delivery_mode       = "none"
    ssl_mode                   = "required"
    glue_database              = null
    glue_warehouse_prefix      = "warehouse/iceberg"
    endpoint                   = module.glue.stage_contracts[local.selected_stage].rest_uri
    ingress_mode               = "aws-service-endpoint"
    local_only                 = false
    poc_only                   = true
    implemented_catalog_types  = ["glue"]
    future_catalog_types       = ["rest"]
    future_adapter_shapes      = ["catalog.aws_glue", "catalog.aws_glue_iceberg_rest"]
    trino_support              = ["glue"]
    dagster_support            = ["glue"]
    floe_support               = ["glue"]
    dbt_support                = ["glue"]
    openmetadata_support       = ["glue"]
    catalog_database_fqn       = "aws_glue.lakehouse_${local.selected_stage}"
    # Per-product namespaces and schema FQNs are deliberately absent: Phase 2
    # reconciles them from the descriptors (ADR 0002), and olf/contracts.py
    # derives both from the same inventory when the contract omits them.
  })

  stage_catalog_contracts = {
    for name, binding in module.glue.stage_contracts : name => merge(binding, {
      provider                  = local.aws_provider_name
      implementation            = "catalog.aws_glue"
      adapter                   = "catalog.aws_glue"
      logical_name              = "stage/${name}/catalog"
      catalog_provider          = "aws-glue"
      catalog_type              = "glue"
      catalog_namespace_model   = local.catalog_namespace_model
      auth_mode                 = "aws-sigv4-pod-identity"
      secret_delivery_mode      = "none"
      ssl_mode                  = "required"
      glue_database             = null
      glue_warehouse_prefix     = "warehouse/iceberg"
      endpoint                  = binding.rest_uri
      ingress_mode              = "aws-service-endpoint"
      local_only                = false
      poc_only                  = true
      implemented_catalog_types = ["glue"]
      future_catalog_types      = ["rest"]
      future_adapter_shapes     = ["catalog.aws_glue", "catalog.aws_glue_iceberg_rest"]
      trino_support             = ["glue"]
      dagster_support           = ["glue"]
      floe_support              = ["glue"]
      dbt_support               = ["glue"]
      openmetadata_support      = ["glue"]
      catalog_database_fqn      = "aws_glue.${binding.catalog_name}"
    })
  }

  governance_contract = merge(local.governance_enabled ? module.openmetadata[0].contract : {}, {
    enabled        = local.governance_enabled
    provider       = local.aws_provider_name
    implementation = "governance.openmetadata_on_eks"
    adapter        = "governance.openmetadata_on_eks"
    logical_name   = "governance_catalog"
    auth_mode      = "local-development"
    endpoint       = local.governance_enabled ? "http://${module.openmetadata[0].contract.service_name}.${module.openmetadata[0].contract.service_namespace}:${module.openmetadata[0].contract.http_port}" : null
    ingress_mode   = "cluster-internal"
    local_only     = false
    poc_only       = true
  })

  selected_stage_analytics = contains(keys(local.analytics_stages), local.selected_stage)

  reporting_contract = merge(try(module.superset[local.selected_stage].contract, {}), {
    enabled        = local.selected_stage_analytics
    provider       = local.aws_provider_name
    implementation = "reporting.superset_on_eks"
    adapter        = "reporting.superset_on_eks"
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
    provider          = local.aws_provider_name
    implementation    = "query.trino_on_eks"
    adapter           = "query.trino_on_eks"
    logical_name      = "sql_query"
    service_name      = "trino"
    service_namespace = var.shared_namespace
    http_port         = 8080
    # Namespace-qualified: stage-scoped Dagster and Superset resolve this
    # from their own namespace, where a bare service name would not resolve.
    endpoint            = "http://trino.${var.shared_namespace}:8080"
    catalog_name        = local.catalog_contract.catalog_name
    supported_catalogs  = ["glue"]
    active_catalog_type = local.catalog_contract.catalog_type
    storage_ref         = local.storage_contract.logical_name
    catalog_ref         = local.catalog_contract.logical_name
    ingress_mode        = "cluster-internal"
    future_adapter_shapes = [
      "query.athena",
    ]
  }

  orchestration_contract = {
    provider          = local.aws_provider_name
    implementation    = "orchestration.dagster_on_eks"
    adapter           = "orchestration.dagster_on_eks"
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
    floe_manifest_access_mode = "remote"
    floe_manifest_base_uri    = local.floe_manifest_base_uri
    floe_report_base_uri      = local.floe_report_base_uri
    log_base_uri              = local.log_base_uri
    run_artifact_base_uri     = local.run_artifact_base_uri
    supported_catalogs        = ["glue"]
    active_catalog_type       = local.catalog_contract.catalog_type
    storage_ref               = local.storage_contract.logical_name
    catalog_ref               = local.catalog_contract.logical_name
    artifact_bucket_ref       = "ops_artifacts"
    local_only                = false
    poc_only                  = true
  }

  artifact_registry_contract = {
    provider                  = local.aws_provider_name
    implementation            = "artifacts.aws_ecr"
    adapter                   = "artifacts.aws_ecr"
    logical_name              = "runtime_images"
    project_code_image        = "${var.project_code_image_repository}:${var.project_code_image_tag}"
    project_code_image_policy = var.project_code_image_pull_policy
    superset_image            = "${var.superset_image_repository}:${var.superset_image_tag}"
    superset_image_policy     = var.superset_image_pull_policy
    distribution_mode         = "registry-push"
    target_cluster            = local.kubernetes_platform_contract.cluster_name
    local_only                = false
    poc_only                  = true
  }

  artifact_bucket_contract = {
    provider                 = local.aws_provider_name
    implementation           = "artifacts.aws_s3_bucket"
    adapter                  = "artifacts.aws_s3_bucket"
    logical_name             = "ops_artifacts"
    bucket_name              = module.s3.ops_bucket_name
    artifact_base_uri        = local.artifact_base_uri
    access_mode              = "remote"
    base_uri                 = local.floe_manifest_base_uri
    floe_manifest_base_uri   = local.floe_manifest_base_uri
    floe_report_base_uri     = local.floe_report_base_uri
    log_base_uri             = local.log_base_uri
    run_artifact_base_uri    = local.run_artifact_base_uri
    manifest_uris            = local.domain_floe_manifest_uris
    distribution_mode        = "aws-s3-upload"
    storage_ref              = local.storage_contract.logical_name
    credentials_secret_name  = null
    access_key_id_key        = null
    secret_access_key_key    = null
    local_upload_access_mode = "direct"
    local_only               = false
    poc_only                 = true
  }

  artifact_contract = merge(local.artifact_registry_contract, {
    implementation             = "artifacts.aws_ecr_and_s3"
    adapter                    = "artifacts.aws_ecr_and_s3"
    floe_manifest_access_mode  = local.artifact_bucket_contract.access_mode
    floe_manifest_base_uri     = local.artifact_bucket_contract.base_uri
    floe_manifest_uris         = local.artifact_bucket_contract.manifest_uris
    floe_manifest_distribution = local.artifact_bucket_contract.distribution_mode
    ops_bucket_name            = local.artifact_bucket_contract.bucket_name
    artifact_base_uri          = local.artifact_bucket_contract.artifact_base_uri
    floe_report_base_uri       = local.artifact_bucket_contract.floe_report_base_uri
    log_base_uri               = local.artifact_bucket_contract.log_base_uri
    run_artifact_base_uri      = local.artifact_bucket_contract.run_artifact_base_uri
  })

  secrets_contract = {
    provider              = local.aws_provider_name
    implementation        = "secrets.kubernetes_secret_on_eks"
    adapter               = "secrets.kubernetes_secret_on_eks"
    backend               = "kubernetes"
    delivery_mode         = "env-from-secret"
    rotation_mode         = "manual-poc"
    references_only       = true
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["secrets.aws_secrets_manager_external_secrets"]
  }

  # ADR 0014: the product-level role model, identical on every provider.
  identity_roles = yamldecode(file("${path.root}/../../../../release/identity-roles.yaml"))

  identity_contract = {
    provider          = local.aws_provider_name
    implementation    = "identity.aws_pod_identity"
    adapter           = "identity.aws_pod_identity"
    auth_mode         = "basic-poc"
    oidc_enabled      = false
    oidc_issuer_url   = try(local.foundation_contract.oidc_issuer_url, null)
    workload_identity = "aws-pod-identity"
    local_only        = false
    poc_only          = true
  }

  access_contract = {
    provider              = local.aws_provider_name
    implementation        = "access.kubectl_port_forward"
    adapter               = "access.kubectl_port_forward"
    ingress_mode          = "port-forward"
    internal_access_mode  = "cluster-dns"
    external_access_mode  = "localhost-port-forward"
    tls_mode              = "none-poc"
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["access.aws_load_balancer_controller", "access.private_dns"]
  }

  observability_contract = {
    provider              = local.aws_provider_name
    implementation        = "observability.object_log_archive_on_eks"
    adapter               = "observability.object_log_archive_on_eks"
    metrics_enabled       = false
    tracing_enabled       = false
    logs_mode             = "s3-object-archive"
    log_base_uri          = local.log_base_uri
    compute_log_uri       = "${local.log_base_uri}/dagster/compute"
    kubernetes_log_uri    = "${local.log_base_uri}/k8s"
    artifact_bucket_ref   = local.artifact_bucket_contract.logical_name
    local_only            = false
    poc_only              = true
    future_adapter_shapes = ["observability.cloudwatch", "observability.managed_prometheus"]
  }

  provider_contracts = {
    schema_version = "3.0.0"
    deployment = {
      profile_name = var.profile_name
      provider     = local.aws_provider_name
      region       = local.aws_region
    }
    shared = merge({
      foundation          = { ref = "shared/foundation", implementation = local.foundation_contract.implementation }
      kubernetes_platform = { ref = "shared/kubernetes_platform", implementation = local.kubernetes_platform_contract.implementation }
      metadata_database   = { ref = "shared/metadata_database", implementation = local.metadata_database_contract.implementation }
      query = {
        ref            = "shared/query"
        implementation = local.query_contract.implementation
        endpoint       = local.query_contract.endpoint
      }
      artifact_registry = { ref = "shared/artifact_registry", implementation = local.artifact_registry_contract.implementation }
      ops_storage = {
        ref                      = "shared/ops_storage"
        implementation           = local.artifact_bucket_contract.implementation
        bucket_name              = local.artifact_bucket_contract.bucket_name
        artifact_base_uri        = local.artifact_bucket_contract.artifact_base_uri
        access_mode              = local.artifact_bucket_contract.access_mode
        local_upload_access_mode = local.artifact_bucket_contract.local_upload_access_mode
        # Always null: AWS S3 authenticates through Pod Identity, and olf's
        # own upload path uses --via direct (boto3's own credential chain),
        # never port-forward with a static Secret.
        credentials_secret_name = local.artifact_bucket_contract.credentials_secret_name
        access_key_id_key       = local.artifact_bucket_contract.access_key_id_key
        secret_access_key_key   = local.artifact_bucket_contract.secret_access_key_key
      }
      secrets       = { ref = "shared/secrets", implementation = local.secrets_contract.implementation }
      identity      = { ref = "shared/identity", implementation = local.identity_contract.implementation, roles = local.identity_roles }
      access        = { ref = "shared/access", implementation = local.access_contract.implementation }
      observability = { ref = "shared/observability", implementation = local.observability_contract.implementation }
      }, local.governance_enabled ? {
      # OpenMetadata is one shared instance across every governed stage, so
      # its binding lives here rather than being duplicated per stage.
      governance_service = {
        ref            = "shared/governance_service"
        implementation = local.governance_contract.implementation
        endpoint       = local.governance_contract.endpoint
      }
    } : {})
    stages = {
      for name, stage in local.enabled_stages : name => merge({
        namespace = local.stage_namespaces[name]
        storage = {
          provider          = local.aws_provider_name
          implementation    = "storage.aws_s3"
          protocol          = "s3"
          region            = local.aws_region
          path_style_access = false
          ssl_mode          = "required"
          identity_ref      = "stage/${name}/runtime_identity"
          bronze = {
            physical_id = local.stage_storage_contracts[name].bronze_bucket_name
            bucket_name = local.stage_storage_contracts[name].bronze_bucket_name
            uri         = "s3://${local.stage_storage_contracts[name].bronze_bucket_name}"
          }
          silver = {
            physical_id = local.stage_storage_contracts[name].silver_bucket_name
            bucket_name = local.stage_storage_contracts[name].silver_bucket_name
            uri         = "s3://${local.stage_storage_contracts[name].silver_bucket_name}"
          }
          gold = {
            physical_id = local.stage_storage_contracts[name].gold_bucket_name
            bucket_name = local.stage_storage_contracts[name].gold_bucket_name
            uri         = "s3://${local.stage_storage_contracts[name].gold_bucket_name}"
          }
        }
        catalog = {
          logical_name     = "iceberg_catalog"
          implementation   = local.stage_catalog_contracts[name].implementation
          catalog_type     = "glue"
          catalog_provider = "aws-glue"
          catalog_name     = local.stage_catalog_contracts[name].catalog_name
          runtime_profile  = "aws-glue-rest"
          physical_id      = local.stage_catalog_contracts[name].glue_catalog_id
          warehouse        = local.stage_catalog_contracts[name].glue_catalog_id
          glue_region      = local.aws_region
          glue_catalog_id  = local.stage_catalog_contracts[name].glue_catalog_id
        }
        query = {
          service_ref          = "shared/query"
          catalog_ref          = "stage/${name}/catalog"
          catalog_name         = local.stage_catalog_contracts[name].catalog_name
          endpoint             = local.query_contract.endpoint
          runtime_identity_ref = "stage/${name}/runtime_identity"
        }
        orchestration = {
          service_ref  = "stage/${name}/orchestration"
          endpoint_ref = "stage/${name}/endpoints/orchestration"
          # The same list the Dagster module renders workspace.yaml from, and
          # the set a stage's user deployments are rendered from.
          # dagster-user-deployments names each Service after its deployment,
          # so a code-location name is also an in-cluster host: a second copy
          # on the activation side points the webserver at a Service nobody
          # creates, and the stage comes up with no code server reachable.
          code_locations = local.orchestration_contract.code_locations
        }
        activation = {
          ops_storage_ref = "shared/ops_storage"
          prefix          = "activations/${name}"
        }
        runtime_identity = {
          ref       = "stage/${name}/runtime_identity"
          principal = local.stage_service_accounts[name]
        }
        endpoints = merge(
          {
            catalog       = "stage/${name}/catalog"
            query         = "shared/query"
            orchestration = "stage/${name}/endpoints/orchestration"
          },
          stage.analytics ? { reporting = "stage/${name}/endpoints/reporting" } : {},
          stage.governance ? { governance = "stage/${name}/endpoints/governance" } : {},
        )
        },
        stage.analytics ? {
          reporting = {
            service_ref  = "stage/${name}/reporting"
            endpoint_ref = "stage/${name}/endpoints/reporting"
          }
        } : {},
        stage.governance ? {
          governance = {
            service_ref  = "shared/governance_service"
            endpoint_ref = "stage/${name}/endpoints/governance"
          }
      } : {})
    }
  }
}

check "foundation_contract_matches_platform_context" {
  assert {
    condition     = local.kubernetes_platform_contract.kube_context == local.foundation_contract.kube_context
    error_message = "The AWS POC platform kube context must come from the AWS foundation contract."
  }
}

check "aws_contract_adapters_are_explicit" {
  assert {
    condition = alltrue([
      local.foundation_contract.provider == local.aws_provider_name && local.foundation_contract.implementation != "" && local.foundation_contract.adapter != "",
      local.kubernetes_platform_contract.provider == local.aws_provider_name && local.kubernetes_platform_contract.implementation != "" && local.kubernetes_platform_contract.adapter != "",
      local.storage_contract.provider == local.aws_provider_name && local.storage_contract.implementation != "" && local.storage_contract.adapter != "",
      local.metadata_database_contract.provider == local.aws_provider_name && local.metadata_database_contract.implementation != "" && local.metadata_database_contract.adapter != "",
      local.catalog_contract.provider == local.aws_provider_name && local.catalog_contract.implementation != "" && local.catalog_contract.adapter != "",
      local.query_contract.provider == local.aws_provider_name && local.query_contract.implementation != "" && local.query_contract.adapter != "",
      local.orchestration_contract.provider == local.aws_provider_name && local.orchestration_contract.implementation != "" && local.orchestration_contract.adapter != "",
      local.governance_contract.provider == local.aws_provider_name && local.governance_contract.implementation != "" && local.governance_contract.adapter != "",
      local.reporting_contract.provider == local.aws_provider_name && local.reporting_contract.implementation != "" && local.reporting_contract.adapter != "",
      local.artifact_registry_contract.provider == local.aws_provider_name && local.artifact_registry_contract.implementation != "" && local.artifact_registry_contract.adapter != "",
      local.artifact_bucket_contract.provider == local.aws_provider_name && local.artifact_bucket_contract.implementation != "" && local.artifact_bucket_contract.adapter != "",
      local.secrets_contract.provider == local.aws_provider_name && local.secrets_contract.implementation != "" && local.secrets_contract.adapter != "",
      local.identity_contract.provider == local.aws_provider_name && local.identity_contract.implementation != "" && local.identity_contract.adapter != "",
      local.access_contract.provider == local.aws_provider_name && local.access_contract.implementation != "" && local.access_contract.adapter != "",
      local.observability_contract.provider == local.aws_provider_name && local.observability_contract.implementation != "" && local.observability_contract.adapter != "",
    ])
    error_message = "Every AWS POC provider contract must declare provider, implementation, and adapter."
  }
}

check "aws_poc_uses_managed_services" {
  assert {
    condition = alltrue([
      local.storage_contract.implementation == "storage.aws_s3",
      local.metadata_database_contract.implementation == "metadata_database.aws_rds_postgresql",
      local.catalog_contract.implementation == "catalog.aws_glue",
      local.artifact_registry_contract.implementation == "artifacts.aws_ecr",
      local.artifact_bucket_contract.distribution_mode == "aws-s3-upload",
    ])
    error_message = "The AWS POC must use S3, RDS PostgreSQL, Glue, ECR, and S3 artifact upload."
  }
}

check "catalog_contract_consumer_support" {
  assert {
    condition = alltrue([
      contains(local.catalog_contract.trino_support, local.catalog_contract.catalog_type),
      contains(local.catalog_contract.dagster_support, local.catalog_contract.catalog_type),
      contains(local.catalog_contract.floe_support, local.catalog_contract.catalog_type),
      contains(local.catalog_contract.dbt_support, local.catalog_contract.catalog_type),
      contains(local.catalog_contract.openmetadata_support, local.catalog_contract.catalog_type),
    ])
    error_message = "The active catalog_type must be declared as supported by Trino, Dagster, Floe, dbt, and OpenMetadata."
  }
}

check "stage_namespaces_are_distinct" {
  assert {
    condition = alltrue([
      length(distinct(values(local.stage_namespaces))) == length(local.stage_namespaces),
      !contains(values(local.stage_namespaces), var.shared_namespace),
    ])
    error_message = "Every enabled stage must own a namespace of its own, distinct from the shared platform namespace."
  }
}

check "stage_services_stay_in_their_own_stage" {
  assert {
    condition = alltrue(concat(
      [for name, instance in module.dagster : instance.namespace == local.stage_namespaces[name]],
      [for name, instance in module.superset : instance.namespace == local.stage_namespaces[name]],
    ))
    error_message = "Each stage-scoped service instance must be deployed in its own stage namespace."
  }
}

check "stage_metadata_state_is_not_shared" {
  assert {
    condition     = length(distinct([for database in values(local.stage_databases) : database.db_name])) == length(local.stage_databases)
    error_message = "Every stage-scoped service instance must own its metadata database; sharing one mixes stage state."
  }
}

check "stage_iam_is_not_shared" {
  assert {
    condition = alltrue([
      length(distinct([for role in values(aws_iam_role.stage_workloads) : role.arn])) == length(aws_iam_role.stage_workloads),
      length(distinct([for arns in values(module.s3.stage_bucket_arns) : join(",", values(arns))])) == length(module.s3.stage_bucket_arns),
    ])
    error_message = "Every stage must have its own IAM role and its own set of S3 bucket ARNs; sharing either breaks IAM-enforced stage isolation."
  }
}
