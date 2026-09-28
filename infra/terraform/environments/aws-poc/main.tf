terraform {
  # >= 1.7.0 for the `removed` block in modules/catalog/aws-glue (ADR 0002):
  # Glue database lifecycle moved to Phase 2, and `removed` is how existing
  # deployments hand those databases to olf without Terraform destroying them.
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.62"
    }
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

provider "aws" {
  region = local.aws_region

  default_tags {
    tags = var.default_tags
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
  aws_region      = coalesce(try(local.foundation_contract.aws_region, null), var.aws_region)
  kubeconfig_path = var.kubeconfig_path != null ? abspath(pathexpand(var.kubeconfig_path)) : coalesce(try(local.foundation_contract.kubeconfig_path, null), abspath("${path.root}/../../../../.tmp/kubeconfigs/aws.yaml"))
  helm_repository_cache_path = (
    var.helm_repository_cache_path != null
    ? abspath(pathexpand(var.helm_repository_cache_path))
    : abspath("${path.root}/../../../../.tmp/helm/aws/repository-cache")
  )
  helm_repository_config_path = (
    var.helm_repository_config_path != null
    ? abspath(pathexpand(var.helm_repository_config_path))
    : abspath("${path.root}/../../../../.tmp/helm/aws/repositories.yaml")
  )
  artifact_base_uri      = "s3://${module.s3.ops_bucket_name}"
  floe_manifest_base_uri = "${local.artifact_base_uri}/floe/manifests"
  floe_report_base_uri   = "${local.artifact_base_uri}/floe/reports"
  log_base_uri           = "${local.artifact_base_uri}/logs"
  run_artifact_base_uri  = "${local.artifact_base_uri}/run-artifacts"
  domain_floe_manifest_uris = {
    sales        = "${local.floe_manifest_base_uri}/sales/sales.manifest.json"
    supply_chain = "${local.floe_manifest_base_uri}/supply_chain/supply_chain.manifest.json"
  }
  # Databases/namespaces themselves are not declared here. `olf catalog
  # sync-namespaces` reconciles them from the lakehouse inventory during
  # artifacts-deploy (ADR 0002); this root only records which naming model
  # those databases follow.
  catalog_namespace_model = "medallion-owner"

  # The resolved topology drives every namespace, service instance, IAM
  # binding, and capability gate below. Nothing here re-reads the Deployment
  # Profile.
  enabled_stages     = { for name, stage in var.stages : name => stage if stage.enabled }
  analytics_stages   = { for name, stage in local.enabled_stages : name => stage if stage.analytics }
  governed_stages    = { for name, stage in local.enabled_stages : name => stage if stage.governance }
  governance_enabled = length([for stage in values(local.enabled_stages) : true if stage.governance]) > 0

  stage_namespaces = { for name in keys(local.enabled_stages) : name => "olf-${name}" }

  selected_stage           = contains(keys(local.enabled_stages), "dev") ? "dev" : sort(keys(local.enabled_stages))[0]
  selected_stage_namespace = local.stage_namespaces[local.selected_stage]

  governance_dagster_stage = contains(keys(local.governed_stages), local.selected_stage) ? local.selected_stage : try(sort(keys(local.governed_stages))[0], local.selected_stage)
  # The logical Trino/dbt identity for this stage's Dagster runtime
  # (OPENLAKEFORGE_DBT_TRINO_USER, query_contract.runtime_identity_principal
  # below) - not a Kubernetes identity. Must match the Trino catalog access
  # rule's user pattern (modules/query/trino/main.tf: "olf-<stage>-runtime"),
  # the same convention local/azure already use - a mismatch here is exactly
  # what Trino's catalog rule is keyed on to tell stages apart. The Dagster
  # Helm chart's `global.serviceAccountName` is a separate, unrelated fixed
  # literal "dagster" regardless of release name or stage (modules/
  # orchestration/dagster/release.tf), so the per-stage
  # aws_eks_pod_identity_association below binds that bare name,
  # namespace-scoped instead of name-scoped.
  stage_service_accounts = { for name in keys(local.enabled_stages) : name => "olf-${name}-runtime" }
  stage_buckets = {
    for name in keys(local.enabled_stages) : name => {
      # The old AWS POC owned the unqualified Bronze/Silver/Gold buckets at
      # the module's `bronze`/`silver`/`gold` addresses. Keep those physical
      # DEV names (and move the addresses in aws-s3) so this platform upgrade
      # cannot replace force-destroyable buckets; UAT/PROD get new identities.
      bronze_bucket_name    = name == "dev" ? var.bronze_bucket_name : "${var.profile_name}-${name}-bronze",
      silver_bucket_name    = name == "dev" ? var.silver_bucket_name : "${var.profile_name}-${name}-silver",
      gold_bucket_name      = name == "dev" ? var.gold_bucket_name : "${var.profile_name}-${name}-gold",
      preserve_legacy_names = name == "dev"
    }
  }
  stage_catalogs_desired = {
    for name in keys(local.enabled_stages) : name => {
      catalog_name = "lakehouse_${name}"
    }
  }
  legacy_stage_database_identities = {
    dev = {
      dagster = {
        db_name                 = "dagster"
        db_user                 = "dagster"
        credentials_secret_name = "dagster-postgresql-secret"
      }
      superset = {
        db_name                 = "superset"
        db_user                 = "superset"
        credentials_secret_name = "superset-postgresql"
      }
    }
  }
  stage_labels = { for name in keys(local.enabled_stages) : name => {
    "openlakeforge.io/stage"      = name
    "openlakeforge.io/managed-by" = "openlakeforge"
    "openlakeforge.io/profile"    = var.profile_name
  } }

  stage_databases = merge(
    {
      for name in keys(local.enabled_stages) : "dagster_${name}" => {
        key = "dagster_${name}"
        # The v0.2 POC used these unqualified DEV identities. Reusing them
        # lets the new stage contract adopt the existing RDS history instead
        # of switching Dagster to a blank database during the platform apply.
        db_name                 = try(local.legacy_stage_database_identities[name].dagster.db_name, "dagster_${name}")
        db_user                 = try(local.legacy_stage_database_identities[name].dagster.db_user, "dagster_${name}")
        credentials_secret_name = try(local.legacy_stage_database_identities[name].dagster.credentials_secret_name, "postgresql-dagster-${name}-creds")
        namespaces              = [local.stage_namespaces[name]]
      }
    },
    {
      for name in keys(local.analytics_stages) : "superset_${name}" => {
        key                     = "superset_${name}"
        db_name                 = try(local.legacy_stage_database_identities[name].superset.db_name, "superset_${name}")
        db_user                 = try(local.legacy_stage_database_identities[name].superset.db_user, "superset_${name}")
        credentials_secret_name = try(local.legacy_stage_database_identities[name].superset.credentials_secret_name, "postgresql-superset-${name}-creds")
        namespaces              = [local.stage_namespaces[name]]
      }
    },
    local.governance_enabled ? {
      openmetadata = {
        key                     = "openmetadata"
        db_name                 = "openmetadata"
        db_user                 = "openmetadata"
        credentials_secret_name = "openmetadata-postgresql"
        namespaces              = [var.shared_namespace]
      }
    } : {},
  )
  # EKS Pod Identity: Trino and OpenMetadata are one shared service reading
  # every enabled stage's Glue catalog and S3 buckets, so their role stays
  # broad by necessity (Trino enforces per-stage isolation itself, via its
  # own file-based access control - see modules/query/trino). Dagster is
  # stage-scoped and gets its own per-stage role below, confined to only its
  # own stage's bucket ARNs and Glue catalog ARN: on AWS this is real IAM
  # enforcement, not app-level configuration.
  shared_service_accounts = ["trino", "openmetadata", "openmetadata-bootstrap"]
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

# EKS does not ship a default StorageClass (kind does), so PVCs created without an
# explicit class never bind. Provide a gp3 default backed by the EBS CSI driver
# (which authenticates via Pod Identity). WaitForFirstConsumer provisions the
# volume in the consuming pod's availability zone.
resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type = "gp3"
  }
}

module "s3" {
  source = "../../modules/storage/aws-s3"

  bucket_name_prefix = var.bucket_name_prefix
  region             = local.aws_region
  ops_bucket_name    = var.ops_bucket_name
  stage_buckets      = local.stage_buckets
}

module "glue" {
  source = "../../modules/catalog/aws-glue"

  region         = local.aws_region
  account_id     = local.foundation_contract.aws_account_id
  stage_catalogs = local.stage_catalogs_desired
}

module "rds_postgresql" {
  source = "../../modules/storage/rds-postgresql"

  namespace           = kubernetes_namespace_v1.shared.metadata[0].name
  name_prefix         = "openlakeforge"
  vpc_id              = local.foundation_contract.vpc_id
  subnet_ids          = local.foundation_contract.subnet_ids
  allowed_cidr_blocks = [local.foundation_contract.vpc_cidr_block]
  instance_class      = var.rds_instance_class
  databases           = values(local.stage_databases)

  depends_on = [
    kubernetes_namespace_v1.stage,
  ]
}

# Preserve the v0.2 passwords while moving their Secrets into the stage/shared
# namespaces. The bootstrap job then adopts the existing RDS databases and
# roles instead of resetting credentials or starting DEV services from empty
# metadata stores.
moved {
  from = module.rds_postgresql.random_password.dagster
  to   = module.rds_postgresql.random_password.database["dagster_dev"]
}

moved {
  from = module.rds_postgresql.random_password.superset[0]
  to   = module.rds_postgresql.random_password.database["superset_dev"]
}

moved {
  from = module.rds_postgresql.random_password.openmetadata[0]
  to   = module.rds_postgresql.random_password.database["openmetadata"]
}

moved {
  from = module.rds_postgresql.kubernetes_secret_v1.dagster
  to   = module.rds_postgresql.kubernetes_secret_v1.database_credentials["dagster_dev/olf-dev"]
}

moved {
  from = module.rds_postgresql.kubernetes_secret_v1.superset[0]
  to   = module.rds_postgresql.kubernetes_secret_v1.database_credentials["superset_dev/olf-dev"]
}

moved {
  from = module.rds_postgresql.kubernetes_secret_v1.openmetadata[0]
  to   = module.rds_postgresql.kubernetes_secret_v1.database_credentials["openmetadata/olf-system"]
}

# The broad, shared-service IAM role: Trino (every stage's catalog/buckets)
# and OpenMetadata (governance across every stage). Never bound to a
# stage-scoped Dagster service account - see aws_iam_role.stage below.
resource "aws_iam_policy" "shared_workloads" {
  name        = "${local.foundation_contract.cluster_name}-openlakeforge-shared"
  description = "OpenLakeForge AWS POC shared-service (Trino/OpenMetadata) access to every stage's S3 and Glue resources"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:ListBucketMultipartUploads",
        ]
        Resource = concat(
          flatten([for arns in values(module.s3.stage_bucket_arns) : values(arns)]),
          [module.s3.ops_bucket_arn],
        )
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
        ]
        Resource = concat(
          [for arn in flatten([for arns in values(module.s3.stage_bucket_arns) : values(arns)]) : "${arn}/*"],
          ["${module.s3.ops_bucket_arn}/*"],
        )
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetCatalog",
          "glue:GetCatalogs",
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:CreateDatabase",
          "glue:UpdateDatabase",
          "glue:DeleteDatabase",
          "glue:GetTable",
          "glue:GetTables",
          "glue:CreateTable",
          "glue:UpdateTable",
          "glue:DeleteTable",
          "glue:GetPartition",
          "glue:GetPartitions",
          "glue:BatchGetPartition",
          "glue:CreatePartition",
          "glue:UpdatePartition",
          "glue:DeletePartition",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role" "shared_workloads" {
  name = "${local.foundation_contract.cluster_name}-openlakeforge-shared"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "pods.eks.amazonaws.com" }
        Action = [
          "sts:AssumeRole",
          "sts:TagSession",
        ]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "shared_workloads" {
  role       = aws_iam_role.shared_workloads.name
  policy_arn = aws_iam_policy.shared_workloads.arn
}

resource "aws_eks_pod_identity_association" "shared_workloads" {
  for_each = toset(local.shared_service_accounts)

  cluster_name    = local.foundation_contract.cluster_name
  namespace       = kubernetes_namespace_v1.shared.metadata[0].name
  service_account = each.value
  role_arn        = aws_iam_role.shared_workloads.arn
}

# One IAM role per stage, scoped to only that stage's three bucket ARNs and
# its own Glue catalog ARN - the real isolation boundary on AWS (enforced by
# IAM itself, not app configuration). Bound to that stage's Dagster
# service accounts (Floe and dbt inherit this identity, since both run
# inside the Dagster pod).
resource "aws_iam_policy" "stage_workloads" {
  for_each = local.enabled_stages

  name        = "${local.foundation_contract.cluster_name}-openlakeforge-${each.key}"
  description = "OpenLakeForge AWS POC ${each.key}-stage workload access to its own S3 buckets and Glue catalog only"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:ListBucketMultipartUploads",
        ]
        Resource = values(module.s3.stage_bucket_arns[each.key])
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
        ]
        Resource = [for arn in values(module.s3.stage_bucket_arns[each.key]) : "${arn}/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        # Dagster/Floe upload the stage's own activation artifacts (manifests,
        # reports, run logs) into the shared ops bucket under its own prefix
        # only - never another stage's.
        Resource = [
          "${module.s3.ops_bucket_arn}/activations/${each.key}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [module.s3.ops_bucket_arn]
        Condition = {
          StringLike = {
            "s3:prefix" = ["activations/${each.key}/*"]
          }
        }
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        # Floe manifests/revisions are published at the ops bucket root
        # (immutable, content-addressed - libs/floe_revision.py, olf/revision.py),
        # deliberately independent of any stage's activations/<stage> prefix.
        # Every stage's Dagster runtime must read them regardless of which
        # stage published the currently-active revision.
        Resource = [
          "${module.s3.ops_bucket_arn}/floe/manifests/*",
          "${module.s3.ops_bucket_arn}/floe/revisions/*",
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        # The Floe binary's own run report_base (baked into each manifest
        # from the same bucket-root floe_manifest_base_uri local as above)
        # is keyed by domain, not by stage - one more artifact this POC's
        # single-stage topology keeps out of the activations/<stage> prefix.
        Resource = ["${module.s3.ops_bucket_arn}/floe/reports/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetCatalog",
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:CreateDatabase",
          "glue:UpdateDatabase",
          "glue:DeleteDatabase",
          "glue:GetTable",
          "glue:GetTables",
          "glue:CreateTable",
          "glue:UpdateTable",
          "glue:DeleteTable",
          "glue:GetPartition",
          "glue:GetPartitions",
          "glue:BatchGetPartition",
          "glue:CreatePartition",
          "glue:UpdatePartition",
          "glue:DeletePartition",
        ]
        # This account's Glue service refuses to create a custom catalog per
        # stage, so every stage shares the account's one default catalog
        # (bare "catalog" ARN, no trailing name). Isolation instead comes
        # from the physical database name: olf.contracts.resolve_physical_
        # names prefixes every database this stage owns with its own
        # catalog_name ("lakehouse_dev_sales_silver", never bare
        # "sales_silver"), so a `database/<prefix>_*` / `table/<prefix>_*`
        # wildcard is what actually scopes this policy to one stage.
        Resource = [
          "arn:aws:glue:${local.aws_region}:${local.foundation_contract.aws_account_id}:catalog",
          "arn:aws:glue:${local.aws_region}:${local.foundation_contract.aws_account_id}:database/${local.stage_catalogs_desired[each.key].catalog_name}_*",
          "arn:aws:glue:${local.aws_region}:${local.foundation_contract.aws_account_id}:table/${local.stage_catalogs_desired[each.key].catalog_name}_*/*",
        ]
      },
    ]
  })
}

resource "aws_iam_role" "stage_workloads" {
  for_each = local.enabled_stages

  name = "${local.foundation_contract.cluster_name}-openlakeforge-${each.key}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "pods.eks.amazonaws.com" }
        Action = [
          "sts:AssumeRole",
          "sts:TagSession",
        ]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "stage_workloads" {
  for_each = local.enabled_stages

  role       = aws_iam_role.stage_workloads[each.key].name
  policy_arn = aws_iam_policy.stage_workloads[each.key].arn
}

resource "aws_eks_pod_identity_association" "stage_workloads" {
  for_each = local.enabled_stages

  cluster_name = local.foundation_contract.cluster_name
  namespace    = kubernetes_namespace_v1.stage[each.key].metadata[0].name
  # Every Dagster pod in this namespace (webserver, daemon, user-deployments,
  # and k8s-run-launcher job pods) runs under the same bare "dagster" SA -
  # the chart's `global.serviceAccountName` (release.tf), not release- or
  # stage-derived. The stage namespace is what disambiguates this
  # association from every other stage's, not the SA name.
  service_account = "dagster"
  role_arn        = aws_iam_role.stage_workloads[each.key].arn
}

module "trino" {
  source = "../../modules/query/trino"

  namespace                  = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file           = "${path.root}/../../../helm/values/local/trino.yaml"
  chart_package_path         = var.trino_chart_package_path
  storage_contract           = local.storage_contract
  catalog_contract           = local.catalog_contract
  stage_catalog_contracts    = local.stage_catalog_contracts
  catalog_bootstrap_revision = "aws-glue"
  # EKS Pod Identity binds the shared workload role to the "trino" service
  # account (aws_eks_pod_identity_association.shared_workloads). Force the
  # Trino chart to create and run under that SA; annotations stay empty
  # under Pod Identity.
  service_account_name = "trino"

  depends_on = [
    module.glue,
    aws_iam_role_policy_attachment.shared_workloads,
    aws_eks_pod_identity_association.shared_workloads,
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
  # OpenMetadata currently has one active Iceberg connection. Its binding
  # follows the governed stage rather than the selected runtime stage, and the
  # check below fails closed until #131 can configure multiple connections.
  catalog_contract = merge(
    local.catalog_contract,
    local.stage_catalog_contracts[local.governance_dagster_stage],
  )
  storage_contract = merge(
    local.storage_contract,
    local.stage_storage_contracts[local.governance_dagster_stage],
  )
  postgresql_contract = local.metadata_database_contract
  postgresql_ssl_mode = "require"
  # Empty by design: the database schemas mirror Glue databases, which now
  # come into existence in Phase 2. `olf openmetadata deploy-metadata` creates
  # each databaseSchema entity right before it seeds that schema's tables.
  catalog_schema_names    = []
  catalog_refresh_enabled = false
  workload_namespaces     = [for name in keys(local.governed_stages) : local.stage_namespaces[name]]
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
    module.glue,
    module.rds_postgresql,
    aws_eks_pod_identity_association.shared_workloads,
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
  postgresql_ssl_mode = "require"

  depends_on = [
    module.rds_postgresql,
    module.trino,
  ]
}

moved {
  from = module.openmetadata
  to   = module.openmetadata[0]
}

module "dagster" {
  source   = "../../modules/orchestration/dagster"
  for_each = local.enabled_stages

  # Bare "dagster" (the module default), same as local/azure-poc - the
  # release's own namespace already disambiguates stages, and the artifact
  # pipeline's k8s.set_project_code_image() (olf/k8s.py) only knows how to
  # patch that fixed set of chart-generated names; a per-stage release name
  # here made it silently skip every AWS deployment's rollout.
  namespace                      = kubernetes_namespace_v1.stage[each.key].metadata[0].name
  chart_package_path             = var.dagster_chart_package_path
  base_values_file               = "${path.root}/../../../helm/values/local/dagster.yaml"
  project_code_image_repository  = var.project_code_image_repository
  project_code_image_tag         = var.project_code_image_tag
  project_code_image_pull_policy = var.project_code_image_pull_policy
  project_code_image_revision    = var.project_code_image_revision
  manage_user_deployments        = var.manage_user_deployments
  storage_contract               = local.stage_storage_contracts[each.key]
  catalog_contract               = local.stage_catalog_contracts[each.key]
  governance_contract = merge(local.governance_contract, {
    enabled = local.governance_enabled && each.value.governance
  })
  query_contract = merge(local.query_contract, {
    catalog_name               = local.stage_catalog_contracts[each.key].catalog_name
    runtime_identity_principal = local.stage_service_accounts[each.key]
  })
  postgresql_contract = local.stage_metadata_database_contracts[each.key]
  postgresql_ssl_mode = "require"
  code_locations      = local.orchestration_contract.code_locations
  # Floe manifests are published at the ops bucket root (immutable revision,
  # not a stage activation - see olf.revision and libs/product_dagster.py's
  # _remote_manifest_uri), not under any stage's activations/<stage> prefix.
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
    module.rds_postgresql,
    module.superset,
    aws_eks_pod_identity_association.stage_workloads,
  ]
}
