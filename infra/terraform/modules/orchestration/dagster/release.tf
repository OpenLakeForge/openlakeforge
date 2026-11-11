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
      # guard. Only `enableSubchart` moves the deployments themselves out:
      # `deploy_revision` owns every code location (ADR 0006).
      "dagster-user-deployments" = {
        enabled        = true
        enableSubchart = false
        serviceAccount = {
          annotations = var.service_account_annotations
        }
        deployments = []
      }

      # The webserver and daemon images stay where the catalog pins them, in
      # `base_values_file`: overriding them with a project-code reference here
      # is what made a platform apply depend on a project build existing.
      dagsterWebserver = {
        # The contract's code-location name is also its in-cluster host:
        # dagster-user-deployments names each Service after its deployment.
        workspace = {
          enabled = true
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
          k8sRunLauncher = {
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
                # The UID is the one images/project-code/Dockerfile creates.
                securityContext = {
                  runAsNonRoot = true
                  runAsUser    = 10001
                  runAsGroup   = 10001
                  seccompProfile = {
                    type = "RuntimeDefault"
                  }
                }
              }
              containerConfig = {
                securityContext = {
                  runAsNonRoot             = true
                  runAsUser                = 10001
                  allowPrivilegeEscalation = false
                  capabilities = {
                    drop = ["ALL"]
                  }
                }
                resources = var.run_pod_resources
                env       = local.runtime_env
                envFrom = [
                  for secret in local.runtime_env_secrets : {
                    secretRef = {
                      name = secret.name
                    }
                  }
                ]
              }
            }
          }
        }
      }
    }),
  ])
}
