output "provider_contracts" {
  description = "Provider contract v3 document: deployment, shared bindings and one block per enabled stage."
  value = {
    schema_version = "3.0.0"
    deployment = {
      profile_name = var.profile_name
      provider     = var.provider_name
      region       = var.region
    }
    shared = merge(
      {
        foundation          = { ref = "shared/foundation", implementation = var.implementations.foundation }
        kubernetes_platform = { ref = "shared/kubernetes_platform", implementation = var.implementations.kubernetes_platform }
        metadata_database   = { ref = "shared/metadata_database", implementation = var.implementations.metadata_database }
        query = {
          ref            = "shared/query"
          implementation = var.query.implementation
          endpoint       = var.query.endpoint
        }
        artifact_registry = { ref = "shared/artifact_registry", implementation = var.implementations.artifact_registry }
        ops_storage = {
          ref                      = "shared/ops_storage"
          implementation           = var.ops_storage.implementation
          bucket_name              = var.ops_storage.bucket_name
          artifact_base_uri        = var.ops_storage.artifact_base_uri
          access_mode              = var.ops_storage.access_mode
          local_upload_access_mode = var.ops_storage.local_upload_access_mode
          credentials_secret_name  = var.ops_storage.credentials_secret_name
          access_key_id_key        = var.ops_storage.access_key_id_key
          secret_access_key_key    = var.ops_storage.secret_access_key_key
        }
        secrets = { ref = "shared/secrets", implementation = var.implementations.secrets }
        identity = merge(
          { ref = "shared/identity", implementation = var.identity.implementation, roles = var.identity.roles },
          var.identity.extra,
        )
        access        = merge({ ref = "shared/access", implementation = var.access.implementation }, var.access.extra)
        observability = { ref = "shared/observability", implementation = var.implementations.observability }
      },
      # Filtered for expressions, not `cond ? {...} : {}`: Terraform cannot
      # unify an empty object with one holding nested objects.
      {
        for key, value in {
          catalog_service = var.catalog_service == null ? null : {
            ref            = "shared/catalog_service"
            implementation = var.catalog_service.implementation
            endpoint       = var.catalog_service.endpoint
          }
          portal = var.portal_implementation == null ? null : {
            ref            = "shared/portal"
            implementation = var.portal_implementation
          }
          governance_service = var.governance_service == null ? null : {
            ref            = "shared/governance_service"
            implementation = var.governance_service.implementation
            endpoint       = var.governance_service.endpoint
          }
        } : key => value if value != null
      },
    )
    stages = {
      for name, stage in var.stages : name => merge({
        namespace = stage.namespace
        storage   = stage.storage
        catalog   = stage.catalog
        query = merge({
          service_ref          = "shared/query"
          catalog_ref          = "stage/${name}/catalog"
          catalog_name         = stage.query_catalog_name
          endpoint             = var.query.endpoint
          runtime_identity_ref = "stage/${name}/runtime_identity"
        }, stage.query_extra)
        orchestration = merge({
          service_ref  = "stage/${name}/orchestration"
          endpoint_ref = "stage/${name}/endpoints/orchestration"
          # dagster-user-deployments names each Service after its deployment,
          # so a code-location name is also an in-cluster host: a second copy
          # on the activation side points the webserver at a Service nobody
          # creates, and the stage comes up with no code server reachable.
          code_locations = var.code_locations
          }, {
          for key, value in { pipeline_service_name = stage.pipeline_service_name } : key => value if value != null
        })
        activation = {
          ops_storage_ref = "shared/ops_storage"
          prefix          = "activations/${name}"
        }
        runtime_identity = {
          ref       = "stage/${name}/runtime_identity"
          principal = stage.service_account
        }
        endpoints = merge(
          {
            catalog       = stage.catalog_endpoint_ref
            query         = "shared/query"
            orchestration = "stage/${name}/endpoints/orchestration"
          },
          stage.analytics ? { reporting = "stage/${name}/endpoints/reporting" } : {},
          stage.governance ? { governance = "stage/${name}/endpoints/governance" } : {},
        )
        },
        stage.analytics ? {
          reporting = merge({
            service_ref  = "stage/${name}/reporting"
            endpoint_ref = "stage/${name}/endpoints/reporting"
            }, {
            for key, value in { dashboard_service_name = stage.dashboard_service_name } : key => value if value != null
          })
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

output "stage_metadata_database_contracts" {
  description = "Per-stage metadata database contract: the shared one with that stage's own Dagster (and, for analytics stages, Superset) database, user and credentials Secret."
  value = {
    for name, stage in var.stages : name => merge(
      var.metadata_database,
      {
        dagster_db_name                 = var.stage_databases["dagster_${name}"].db_name
        dagster_db_user                 = var.stage_databases["dagster_${name}"].db_user
        dagster_credentials_secret_name = var.stage_databases["dagster_${name}"].credentials_secret_name
      },
      stage.analytics ? {
        superset_db_name                 = var.stage_databases["superset_${name}"].db_name
        superset_db_user                 = var.stage_databases["superset_${name}"].db_user
        superset_credentials_secret_name = var.stage_databases["superset_${name}"].credentials_secret_name
      } : {},
    )
  }
}
