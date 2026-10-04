data "terraform_remote_state" "local_foundation" {
  backend = "local"

  config = {
    path = local.foundation_state_path
  }
}

locals {
  local_provider_name   = "local"
  foundation_state_path = var.foundation_state_path != null ? abspath(pathexpand(var.foundation_state_path)) : abspath("${path.root}/../../foundations/local-kind/terraform.tfstate")

  foundation_contract = merge(data.terraform_remote_state.local_foundation.outputs.foundation_contract, {
    provider              = local.local_provider_name
    implementation        = "foundation.kind"
    adapter               = "foundation.kind"
    cluster_type          = "kind"
    network_model         = "docker-bridge"
    platform_state_model  = "separate-terraform-root"
    platform_apply_order  = "foundation-before-platform"
    supported_environment = "local-dev"
  })

  kubernetes_platform_contract = {
    provider       = local.local_provider_name
    implementation = "kubernetes.kind"
    adapter        = "platform.kubernetes.kind"
    # The stage a runtime consumer resolves. Shared services live in
    # `shared_namespace`; #114 replaces this single-stage view with the
    # provider-contract v3 stage index.
    namespace            = local.selected_stage_namespace
    shared_namespace     = var.shared_namespace
    stage                = local.selected_stage
    stage_namespaces     = local.stage_namespaces
    kube_context         = coalesce(try(local.foundation_contract.kube_context, null), var.kube_context)
    kubeconfig_path      = coalesce(try(local.foundation_contract.kubeconfig_path, null), local.kubeconfig_path)
    cluster_name         = try(local.foundation_contract.cluster_name, "openlakeforge-local")
    platform_apply_model = "foundation-state-kube-context"
    workload_identity    = "kubernetes-service-account"
  }

  stage_storage_contracts = {
    for name, binding in local.stage_storage : name => merge(module.seaweedfs.contract, {
      provider                = local.local_provider_name
      implementation          = "storage.s3_compatible.seaweedfs"
      adapter                 = "storage.s3_compatible.seaweedfs"
      logical_name            = "stage/${name}/storage"
      protocol                = "s3"
      auth_mode               = "static-access-key-secret"
      secret_delivery_mode    = "kubernetes-secret-env"
      workload_identity       = false
      ssl_mode                = "disabled"
      ingress_mode            = "cluster-internal"
      local_only              = true
      future_adapter_shapes   = ["storage.aws_s3"]
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
    provider              = local.local_provider_name
    implementation        = "storage.s3_compatible.seaweedfs"
    adapter               = "storage.s3_compatible.seaweedfs"
    logical_name          = "lakehouse_storage"
    protocol              = "s3"
    auth_mode             = "static-access-key-secret"
    secret_delivery_mode  = "kubernetes-secret-env"
    workload_identity     = false
    ssl_mode              = "disabled"
    ingress_mode          = "cluster-internal"
    local_only            = true
    future_adapter_shapes = ["storage.aws_s3"]
    bronze_bucket_name    = local.stage_storage[local.selected_stage].bronze_bucket_name
    silver_bucket_name    = local.stage_storage[local.selected_stage].silver_bucket_name
    gold_bucket_name      = local.stage_storage[local.selected_stage].gold_bucket_name
  })

  # One contract per stage-scoped service instance: the shared PostgreSQL
  # server with that stage's own database, user, and credentials Secret.
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

  # The shared OpenMetadata database, flattened onto the contract the
  # governance module's typed input expects. Stage-scoped services take their
  # own database through `stage_metadata_database_contracts` instead.
  governance_database = local.governance_enabled ? local.stage_databases["openmetadata"] : null

  metadata_database_contract = merge(module.postgresql.contract, {
    openmetadata_db_name                 = try(local.governance_database.db_name, null)
    openmetadata_db_user                 = try(local.governance_database.db_user, null)
    openmetadata_credentials_secret_name = try(local.governance_database.credentials_secret_name, null)

    provider              = local.local_provider_name
    implementation        = "metadata_database.postgresql.in_cluster"
    adapter               = "metadata_database.postgresql.in_cluster"
    engine                = "postgresql"
    logical_name          = "platform_metadata"
    auth_mode             = "static-password-secret"
    secret_delivery_mode  = "kubernetes-secret-env"
    ssl_mode              = "disabled"
    endpoint              = "${module.postgresql.contract.host}:${module.postgresql.contract.port}"
    local_only            = true
    future_adapter_shapes = ["metadata_database.aws_rds_postgresql"]
  })

  catalog_contract = merge(module.polaris.contract, {
    provider                   = local.local_provider_name
    implementation             = "catalog.iceberg_rest.polaris"
    adapter                    = "catalog.iceberg_rest.polaris"
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
    local_only                 = true
    implemented_catalog_types  = ["rest"]
    future_catalog_types       = ["glue"]
    future_adapter_shapes      = ["catalog.aws_glue"]
    trino_support              = ["rest", "glue"]
    dagster_support            = ["rest"]
    floe_support               = ["rest"]
    dbt_support                = ["rest"]
    openmetadata_support       = ["rest"]
    catalog_database_fqn       = "polaris.lakehouse_${local.selected_stage}"
    # Per-product namespaces and schema FQNs are deliberately absent: Phase 2
    # reconciles them from the descriptors (ADR 0002), and olf/contracts.py
    # derives both from the same inventory when the contract omits them.
  })

  stage_catalog_contracts = {
    for name, binding in module.polaris.stage_contracts : name => merge(binding, {
      provider                  = local.local_provider_name
      implementation            = "catalog.iceberg_rest.polaris"
      adapter                   = "catalog.iceberg_rest.polaris"
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
      local_only                = true
      implemented_catalog_types = ["rest"]
      future_catalog_types      = ["glue"]
      future_adapter_shapes     = ["catalog.aws_glue"]
      trino_support             = ["rest", "glue"]
      dagster_support           = ["rest"]
      floe_support              = ["rest"]
      dbt_support               = ["rest"]
      openmetadata_support      = ["rest"]
      catalog_database_fqn      = "polaris.${binding.catalog_name}"
    })
  }

  governance_contract = merge(local.governance_enabled ? module.openmetadata[0].contract : {}, {
    enabled        = local.governance_enabled
    provider       = local.local_provider_name
    implementation = "governance.openmetadata"
    adapter        = "governance.openmetadata"
    logical_name   = "governance_catalog"
    auth_mode      = "local-development"
    endpoint       = local.governance_enabled ? "http://${module.openmetadata[0].contract.service_name}.${module.openmetadata[0].contract.service_namespace}:${module.openmetadata[0].contract.http_port}" : null
    ingress_mode   = "cluster-internal"
    local_only     = true
  })

  selected_stage_analytics = contains(keys(local.analytics_stages), local.selected_stage)

  # `try`, not a conditional: indexing a for_each module with a key it does
  # not have is an error even on the unselected branch of a ternary.
  reporting_contract = merge(try(module.superset[local.selected_stage].contract, {}), {
    enabled        = local.selected_stage_analytics
    provider       = local.local_provider_name
    implementation = "reporting.superset"
    adapter        = "reporting.superset"
    logical_name   = "bi_reporting"
    auth_mode      = "local-development"
    endpoint = try(
      "http://${module.superset[local.selected_stage].contract.service_name}.${local.selected_stage_namespace}:${module.superset[local.selected_stage].contract.http_port}",
      null,
    )
    ingress_mode = "cluster-internal"
    local_only   = true
  })

  query_contract = {
    provider          = local.local_provider_name
    implementation    = "query.trino"
    adapter           = "query.trino"
    logical_name      = "sql_query"
    service_name      = "trino"
    service_namespace = var.shared_namespace
    http_port         = 8080
    # Namespace-qualified: stage-scoped Dagster and Superset resolve this
    # from their own namespace, where a bare service name would not resolve.
    endpoint            = "http://trino.${var.shared_namespace}:8080"
    catalog_name        = local.catalog_contract.trino_catalog_name
    supported_catalogs  = ["rest", "glue"]
    active_catalog_type = local.catalog_contract.catalog_type
    storage_ref         = local.storage_contract.logical_name
    catalog_ref         = local.catalog_contract.logical_name
    ingress_mode        = "cluster-internal"
    future_adapter_shapes = [
      "query.trino",
    ]
  }

  orchestration_contract = {
    provider          = local.local_provider_name
    implementation    = "orchestration.dagster"
    adapter           = "orchestration.dagster"
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
    supported_catalogs        = ["rest"]
    active_catalog_type       = local.catalog_contract.catalog_type
    storage_ref               = local.storage_contract.logical_name
    catalog_ref               = local.catalog_contract.logical_name
    artifact_bucket_ref       = "ops_artifacts"
    local_only                = true
    future_adapter_shapes     = ["orchestration.dagster"]
  }

  artifact_registry_contract = {
    provider                  = local.local_provider_name
    implementation            = "artifacts.local_kind_image_load"
    adapter                   = "artifacts.local_kind_image_load"
    logical_name              = "runtime_images"
    project_code_image        = "${var.project_code_image_repository}:${var.project_code_image_tag}"
    project_code_image_policy = var.project_code_image_pull_policy
    superset_image            = "${var.superset_image_repository}:${var.superset_image_tag}"
    superset_image_policy     = var.superset_image_pull_policy
    distribution_mode         = "kind-load"
    target_cluster            = local.kubernetes_platform_contract.cluster_name
    local_only                = true
    future_adapter_shapes     = ["artifacts.ecr"]
  }

  artifact_bucket_contract = {
    provider                 = local.local_provider_name
    implementation           = "artifacts.s3_compatible_bucket"
    adapter                  = "artifacts.local_s3_compatible_bucket"
    logical_name             = "ops_artifacts"
    bucket_name              = var.ops_bucket_name
    artifact_base_uri        = local.artifact_base_uri
    access_mode              = "remote"
    base_uri                 = local.floe_manifest_base_uri
    floe_manifest_base_uri   = local.floe_manifest_base_uri
    floe_report_base_uri     = local.floe_report_base_uri
    log_base_uri             = local.log_base_uri
    run_artifact_base_uri    = local.run_artifact_base_uri
    manifest_uris            = local.domain_floe_manifest_uris
    distribution_mode        = "s3-compatible-upload"
    storage_ref              = local.storage_contract.logical_name
    credentials_secret_name  = local.storage_contract.credentials_secret_name
    access_key_id_key        = local.storage_contract.access_key_id_key
    secret_access_key_key    = local.storage_contract.secret_access_key_key
    local_upload_access_mode = "kubectl-port-forward"
    local_only               = true
    future_adapter_shapes    = ["artifacts.aws_s3"]
  }

  artifact_contract = merge(local.artifact_registry_contract, {
    implementation             = "artifacts.local_kind_and_s3"
    adapter                    = "artifacts.local_kind_and_s3"
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
    provider              = local.local_provider_name
    implementation        = "secrets.kubernetes_secret"
    adapter               = "secrets.kubernetes_secret"
    backend               = "kubernetes"
    delivery_mode         = "env-from-secret"
    rotation_mode         = "manual-development"
    references_only       = true
    local_only            = true
    future_adapter_shapes = ["secrets.aws_secrets_manager_or_external_secrets"]
  }

  identity_contract = {
    provider              = local.local_provider_name
    implementation        = "identity.local_development_credentials"
    adapter               = "identity.local_development_credentials"
    auth_mode             = "basic-local"
    oidc_enabled          = false
    workload_identity     = "kubernetes-service-account"
    local_only            = true
    future_adapter_shapes = ["identity.oidc", "identity.aws_iam_pod_identity"]
  }

  # ADR 0013: user-facing services only, keyed by contract ref. A stage route
  # carries its stage label, a shared one none. Disabled capabilities are
  # absent because their module instances are; storage admin, databases and
  # code servers are never listed.
  access_routes = merge(
    {
      for name in keys(local.enabled_stages) : "stage/${name}/orchestration" => {
        host         = "dagster.${name}.${var.access_base_domain}"
        tls_host     = "*.${name}.${var.access_base_domain}"
        namespace    = module.dagster[name].namespace
        service_name = module.dagster[name].webserver_service_name
        service_port = module.dagster[name].webserver_port
      }
    },
    {
      for name in keys(local.analytics_stages) : "stage/${name}/reporting" => {
        host         = "superset.${name}.${var.access_base_domain}"
        tls_host     = "*.${name}.${var.access_base_domain}"
        namespace    = module.superset[name].namespace
        service_name = module.superset[name].service_name
        service_port = module.superset[name].http_port
      }
    },
    {
      "shared/query" = {
        host         = "trino.${var.access_base_domain}"
        tls_host     = "*.${var.access_base_domain}"
        namespace    = kubernetes_namespace_v1.shared.metadata[0].name
        service_name = module.trino.service_name
        service_port = module.trino.http_port
      }
    },
    local.governance_enabled ? {
      "shared/governance_service" = {
        host         = "openmetadata.${var.access_base_domain}"
        tls_host     = "*.${var.access_base_domain}"
        namespace    = kubernetes_namespace_v1.shared.metadata[0].name
        service_name = module.openmetadata[0].contract.service_name
        service_port = module.openmetadata[0].contract.http_port
      }
    } : {},
  )

  access_contract = {
    provider             = local.local_provider_name
    implementation       = "access.ingress"
    adapter              = "access.traefik_cert_manager"
    ingress_class_name   = module.traefik.ingress_class_name
    cluster_issuer_name  = module.cert_manager.cluster_issuer_name
    base_domain          = var.access_base_domain
    issuer               = var.access_issuer
    tls_mode             = "ingress-terminated"
    internal_access_mode = "cluster-dns"
    routes = {
      for ref, route in local.access_routes : ref => { url = "https://${route.host}", enabled = true, exposure = "user-facing" }
    }
    local_only = true
  }

  observability_contract = {
    provider              = local.local_provider_name
    implementation        = "observability.object_log_archive"
    adapter               = "observability.object_log_archive"
    metrics_enabled       = false
    tracing_enabled       = false
    logs_mode             = "s3-compatible-object-archive"
    log_base_uri          = local.log_base_uri
    compute_log_uri       = "${local.log_base_uri}/dagster/compute"
    kubernetes_log_uri    = "${local.log_base_uri}/k8s"
    artifact_bucket_ref   = local.artifact_bucket_contract.logical_name
    local_only            = true
    future_adapter_shapes = ["observability.loki_grafana", "observability.managed_prometheus", "observability.cloudwatch"]
  }

  provider_contracts = {
    schema_version = "3.0.0"
    deployment = {
      profile_name = var.profile_name
      provider     = local.local_provider_name
      region       = null
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
      catalog_service = {
        ref            = "shared/catalog_service"
        implementation = local.catalog_contract.implementation
        endpoint       = local.catalog_contract.endpoint
      }
      artifact_registry = { ref = "shared/artifact_registry", implementation = local.artifact_registry_contract.implementation }
      ops_storage = {
        ref                      = "shared/ops_storage"
        implementation           = local.artifact_bucket_contract.implementation
        bucket_name              = local.artifact_bucket_contract.bucket_name
        artifact_base_uri        = local.artifact_bucket_contract.artifact_base_uri
        access_mode              = local.artifact_bucket_contract.access_mode
        local_upload_access_mode = local.artifact_bucket_contract.local_upload_access_mode
        # The shared admin identity: olf's own revision-publish/manifest-
        # upload tooling writes outside any stage's activations/<stage>
        # prefix (floe/revisions/...), so it cannot use a stage's own
        # narrower storage identity.
        credentials_secret_name = local.artifact_bucket_contract.credentials_secret_name
        access_key_id_key       = local.artifact_bucket_contract.access_key_id_key
        secret_access_key_key   = local.artifact_bucket_contract.secret_access_key_key
      }
      secrets  = { ref = "shared/secrets", implementation = local.secrets_contract.implementation }
      identity = { ref = "shared/identity", implementation = local.identity_contract.implementation }
      access = {
        ref            = "shared/access"
        implementation = local.access_contract.implementation
        base_domain    = local.access_contract.base_domain
        issuer         = local.access_contract.issuer
        tls_mode       = local.access_contract.tls_mode
        routes         = local.access_contract.routes
      }
      observability = { ref = "shared/observability", implementation = local.observability_contract.implementation }
      }, local.governance_enabled ? {
      # OpenMetadata is one shared instance across every governed stage, so
      # its binding lives here rather than being duplicated per stage - a
      # stage merely references it (see stages[*].governance.service_ref).
      # Only present when governance is enabled anywhere: with no OM instance
      # deployed there is nothing to reference.
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
          provider                = local.local_provider_name
          implementation          = "storage.s3_compatible.seaweedfs"
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
          # Deterministic OpenMetadata pipeline-service root (#131). Present
          # for every enabled stage, not just governed ones: the name itself
          # is a stage property, independent of whether anything registers
          # it. Sourced from stage_databases rather than a literal
          # "dagster_${name}" so the two never drift.
          pipeline_service_name = local.stage_databases["dagster_${name}"].db_name
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
            catalog       = "shared/catalog_service"
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
            # Deterministic OpenMetadata dashboard-service root (#131), same
            # rationale as orchestration.pipeline_service_name above.
            dashboard_service_name = local.stage_databases["superset_${name}"].db_name
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
    error_message = "The local platform kube context must come from the local foundation contract."
  }
}

check "local_contract_adapters_are_explicit" {
  assert {
    condition = alltrue([
      local.foundation_contract.provider == local.local_provider_name && local.foundation_contract.implementation != "" && local.foundation_contract.adapter != "",
      local.kubernetes_platform_contract.provider == local.local_provider_name && local.kubernetes_platform_contract.implementation != "" && local.kubernetes_platform_contract.adapter != "",
      local.storage_contract.provider == local.local_provider_name && local.storage_contract.implementation != "" && local.storage_contract.adapter != "",
      local.metadata_database_contract.provider == local.local_provider_name && local.metadata_database_contract.implementation != "" && local.metadata_database_contract.adapter != "",
      local.catalog_contract.provider == local.local_provider_name && local.catalog_contract.implementation != "" && local.catalog_contract.adapter != "",
      local.query_contract.provider == local.local_provider_name && local.query_contract.implementation != "" && local.query_contract.adapter != "",
      local.orchestration_contract.provider == local.local_provider_name && local.orchestration_contract.implementation != "" && local.orchestration_contract.adapter != "",
      local.governance_contract.provider == local.local_provider_name && local.governance_contract.implementation != "" && local.governance_contract.adapter != "",
      local.reporting_contract.provider == local.local_provider_name && local.reporting_contract.implementation != "" && local.reporting_contract.adapter != "",
      local.artifact_registry_contract.provider == local.local_provider_name && local.artifact_registry_contract.implementation != "" && local.artifact_registry_contract.adapter != "",
      local.artifact_bucket_contract.provider == local.local_provider_name && local.artifact_bucket_contract.implementation != "" && local.artifact_bucket_contract.adapter != "",
      local.secrets_contract.provider == local.local_provider_name && local.secrets_contract.implementation != "" && local.secrets_contract.adapter != "",
      local.identity_contract.provider == local.local_provider_name && local.identity_contract.implementation != "" && local.identity_contract.adapter != "",
      local.access_contract.provider == local.local_provider_name && local.access_contract.implementation != "" && local.access_contract.adapter != "",
      local.observability_contract.provider == local.local_provider_name && local.observability_contract.implementation != "" && local.observability_contract.adapter != "",
    ])
    error_message = "Every local provider contract must declare provider, implementation, and adapter."
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

check "openmetadata_catalog_fqn_uses_lakehouse_database" {
  assert {
    condition     = local.catalog_contract.catalog_database_fqn == "polaris.lakehouse_${local.selected_stage}" && local.catalog_contract.catalog_name != "default"
    error_message = "OpenMetadata catalog assets must resolve under polaris.<catalog_name>, not polaris.default."
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
