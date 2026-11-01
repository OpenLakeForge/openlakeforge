resource "helm_release" "dagster" {
  name       = var.release_name
  repository = var.chart_package_path == null ? var.chart_repository : null
  chart      = var.chart_package_path == null ? "dagster" : var.chart_package_path
  version    = var.chart_version
  namespace  = var.namespace

  # Local project code is a dynamic artifact loaded after Terraform. Do not make
  # static infra apply wait on pods that cannot start until that image exists.
  wait            = false
  timeout         = 300
  cleanup_on_fail = true

  values = compact([
    file(var.base_values_file),
    yamlencode({
      global = {
        serviceAccountName   = "dagster"
        postgresqlSecretName = var.postgresql_contract.dagster_credentials_secret_name
      }

      generatePostgresqlPasswordSecret = false

      postgresql = {
        enabled            = false
        postgresqlHost     = var.postgresql_contract.host
        postgresqlPort     = tostring(var.postgresql_contract.port)
        postgresqlDatabase = var.postgresql_contract.dagster_db_name
        postgresqlUsername = var.postgresql_contract.dagster_db_user
      }

      # `enabled` and `enableSubchart` are not interchangeable: `enabled` is
      # what makes the parent chart render workspace.yaml at all, and turning
      # it off while the subchart condition is still true trips the chart's
      # own "subchart cannot be enabled if workspace.yaml is not created"
      # guard. Only `enableSubchart` moves the deployments themselves out.
      "dagster-user-deployments" = {
        enabled        = true
        enableSubchart = var.manage_user_deployments
        serviceAccount = {
          annotations = var.service_account_annotations
        }
        # Empty when activation owns user code; the subchart renders nothing
        # from it either way once `enableSubchart` is false.
        deployments = local.code_location_deployments
      }

      # The webserver and daemon images stay where the catalog pins them, in
      # `base_values_file`: overriding them with a project-code reference here
      # is what made a platform apply depend on a project build existing.
      dagsterWebserver = {
        # Only when activation owns user code: the parent chart otherwise
        # derives workspace.yaml from the subchart's own deployments, and
        # setting both is rejected. dagster-user-deployments names each
        # Service after its deployment, so the contract's code-location name
        # is also its in-cluster host.
        workspace = {
          enabled = !var.manage_user_deployments
          servers = local.workspace_servers
        }
        env        = local.runtime_env
        envSecrets = local.runtime_env_secrets
      }

      serviceAccount = {
        annotations = var.service_account_annotations
      }

      dagsterDaemon = {
        env        = local.runtime_env
        envSecrets = local.runtime_env_secrets
      }

      computeLogManager = {
        type = "S3ComputeLogManager"
        config = {
          s3ComputeLogManager = {
            bucket = var.artifact_bucket_name
            # A stage's own storage identity is scoped to
            # activations/<stage>/* in the shared ops bucket (#114) - a
            # bucket-root "logs/dagster/compute" prefix falls outside that
            # grant and every compute-log upload is denied. Compute-log
            # calls carry no other stage's activation prefix, so deriving it
            # from log_base_uri (already stage-scoped) keeps this instance's
            # logs inside its own grant.
            localDir       = "/tmp/dagster-compute-logs"
            prefix         = "${trimsuffix(replace(var.log_base_uri, "s3://${var.artifact_bucket_name}/", ""), "/")}/dagster/compute"
            useSsl         = try(var.storage_contract.ssl_mode, null) != "disabled"
            verify         = try(var.storage_contract.ssl_mode, null) != "disabled"
            endpointUrl    = var.storage_contract.endpoint
            region         = var.storage_contract.region
            skipEmptyFiles = true
          }
        }
      }

      runLauncher = {
        type = "K8sRunLauncher"
        config = {
          k8sRunLauncher = merge(
            {
              imagePullPolicy     = var.project_code_image_pull_policy
              jobNamespace        = var.namespace
              loadInclusterConfig = true
              failPodOnRunFailure = true
              runK8sConfig = {
                jobSpecConfig = {
                  ttlSecondsAfterFinished = 3600
                }
                podSpecConfig = {
                  serviceAccountName = "dagster"
                }
                containerConfig = {
                  env = local.runtime_env
                  envFrom = [
                    for secret in local.runtime_env_secrets : {
                      secretRef = {
                        name = secret.name
                      }
                    }
                  ]
                }
              }
            },
            # No `job_image` where activation owns user code (ADR 0006): every
            # run then takes its code location's image through
            # `includeConfigInLaunchedRuns`, and a platform-apply-time value
            # would only name a build no stage activated. The chart renders
            # `job_image` only when this key exists. The deprecated
            # `olf infra deploy` path patches `job_image` in place
            # (`olf infra k8s set-project-code-image`), so it keeps one. A
            # filtered `for` rather than `? {image = ...} : {}`: Terraform
            # cannot unify an object with attributes and one without.
            {
              for key, value in {
                image = {
                  repository = var.project_code_image_repository
                  tag        = var.project_code_image_tag
                  pullPolicy = var.project_code_image_pull_policy
                }
              } : key => value if var.manage_user_deployments
            },
          )
        }
      }
    }),

    # `olf infra deploy` runs the control plane on the project-code image and patches
    # it in place during its artifacts phase (`olf infra k8s set-project-code-image`
    # targets the webserver and daemon by name), so it also needs that image's
    # pull policy -- `Never` locally, where the image only ever exists inside
    # kind. The catalog-pinned control plane in `base_values_file` applies
    # where activation owns user code and nothing patches these deployments.
    var.manage_user_deployments ? yamlencode({
      dagsterWebserver = {
        image = {
          repository = var.project_code_image_repository
          tag        = var.project_code_image_tag
          pullPolicy = var.project_code_image_pull_policy
        }
      }
      dagsterDaemon = {
        image = {
          repository = var.project_code_image_repository
          tag        = var.project_code_image_tag
          pullPolicy = var.project_code_image_pull_policy
        }
      }
    }) : "",
  ])
}
