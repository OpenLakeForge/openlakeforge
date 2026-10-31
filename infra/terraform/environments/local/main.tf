terraform {
  required_version = ">= 1.10.0"

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
  kubeconfig_path = var.kubeconfig_path != null ? abspath(pathexpand(var.kubeconfig_path)) : abspath("${path.root}/../../../../.tmp/kubeconfigs/local.yaml")
  helm_repository_cache_path = (
    var.helm_repository_cache_path != null
    ? abspath(pathexpand(var.helm_repository_cache_path))
    : abspath("${path.root}/../../../../.tmp/helm/local/repository-cache")
  )
  helm_repository_config_path = (
    var.helm_repository_config_path != null
    ? abspath(pathexpand(var.helm_repository_config_path))
    : abspath("${path.root}/../../../../.tmp/helm/local/repositories.yaml")
  )
  artifact_base_uri     = "s3://${var.ops_bucket_name}"
  log_base_uri          = "${local.artifact_base_uri}/logs"
  run_artifact_base_uri = "${local.artifact_base_uri}/run-artifacts"
  # Each stage's Dagster and log archiver write under their own prefix of the
  # ops bucket; the platform API's read-only key is scoped to the same prefixes.
  stage_log_prefixes   = { for name in keys(local.enabled_stages) : name => "activations/${name}/logs" }
  platform_api_enabled = local.identity_enabled && var.platform_api_image != null
  # Catalog namespaces themselves are not declared here. `olf infra catalog
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
  # #114 replaces this with the v3 stage index.
  selected_stage           = contains(keys(local.enabled_stages), "dev") ? "dev" : sort(keys(local.enabled_stages))[0]
  selected_stage_namespace = local.stage_namespaces[local.selected_stage]

  # The canonical governance stage: the one whose catalog OpenMetadata's single
  # Iceberg connection crawls and whose pipeline service receives OpenLineage
  # pipelines. The selected stage need not have enabled governance, so prefer
  # it when it qualifies, else the first stage that does.
  governance_dagster_stage = contains(keys(local.governed_stages), local.selected_stage) ? local.selected_stage : try(sort(keys(local.governed_stages))[0], local.selected_stage)
  stage_service_accounts   = { for name in keys(local.enabled_stages) : name => "olf-${name}-runtime" }
  stage_storage = {
    for name in keys(local.enabled_stages) : name => {
      # The prefix is fixed rather than taken from var.profile_name so the
      # buckets match the `lakehouse_<stage>` catalogs: the default profile
      # name would give `openlakeforge-dev-bronze` next to `lakehouse_dev`.
      bronze_bucket_name      = "lakehouse-${name}-bronze",
      silver_bucket_name      = "lakehouse-${name}-silver",
      gold_bucket_name        = "lakehouse-${name}-gold",
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
    local.identity_dex ? {
      dex = {
        key                     = "dex"
        db_name                 = "dex"
        db_user                 = "dex"
        credentials_secret_name = "postgresql-dex-creds"
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

# The identity a stage's workloads run as. #114 binds storage and catalog
# permissions to it; here it exists so every stage-scoped pod already has a
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
  storage_size       = var.storage_size
  storage_sizes_file = "${path.root}/../../../../release/storage-sizes.yaml"
  chart_package_path = var.seaweedfs_chart_package_path
  bucket_names = concat(flatten([
    for binding in values(local.stage_storage) : [
      binding.bronze_bucket_name,
      binding.silver_bucket_name,
      binding.gold_bucket_name,
    ]
  ]), [var.ops_bucket_name])
  region = var.s3_region
  # Platform credentials stay in olf-system; stage workloads mount only the
  # stage-owned identities supplied through stage_credentials below.
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
  log_reader_credentials = local.platform_api_enabled ? {
    namespace               = kubernetes_namespace_v1.shared.metadata[0].name
    credentials_secret_name = "platform-api-log-reader-s3-creds"
    ops_bucket_name         = var.ops_bucket_name
    prefixes                = [for prefix in values(local.stage_log_prefixes) : "${prefix}/k8s/"]
  } : null

  depends_on = [
    kubernetes_namespace_v1.stage,
  ]
}

module "cert_manager" {
  source = "../../modules/access/cert-manager"
  # Also for Trino's internal certificate (ADR 0016), which needs an issuer
  # even when no route is TLS-terminated.
  count = local.access_enabled || local.trino_workload_authentication != "off" ? 1 : 0

  namespace          = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file   = "${path.root}/../../../helm/values/local/cert-manager.yaml"
  chart_package_path = var.cert_manager_chart_package_path
  base_domain        = local.base_domain
}

module "portal" {
  source = "../../modules/access/portal"
  count  = local.access_enabled ? 1 : 0

  namespace = kubernetes_namespace_v1.shared.metadata[0].name
  links     = local.portal_links
  # GitHub owns accounts and teams; Dex has no account page, so the identity route is not listed.
  account_url  = var.identity_issuer == "github" ? "https://${coalesce(try(var.identity_github.host, null), "github.com")}/orgs/${var.identity_github.organization}/teams" : null
  sign_out_url = local.sign_out_url
}

# ADR 0021: the platform API, at /api on the portal's host. It validates every
# token itself, so it needs an issuer. Its one service credential is the
# read-only object-store key for the log archive: Dagster takes none, and
# OpenMetadata gets the caller's own token.
module "platform_api" {
  source = "../../modules/access/platform-api"
  count  = local.platform_api_enabled ? 1 : 0

  namespace         = kubernetes_namespace_v1.shared.metadata[0].name
  image             = var.platform_api_image
  image_pull_policy = "Never"
  issuer_url        = local.identity_oidc.issuer_url
  # Bearer tokens from olf only: a browser session's token does not reach
  # the API yet (ADR 0021, open item 1).
  audiences         = [local.identity_oidc.clients["olf-cli"].client_id]
  role_claim        = local.identity_oidc.role_claim
  role_mapping      = local.identity_oidc.role_mapping
  precedence        = local.identity_roles.precedence
  stages            = sort(keys(local.enabled_stages))
  ca_configmap_name = local.identity_deployed ? module.in_cluster_resolution[0].ca_configmap_name : null
  # Reached in-cluster over F32 (ADR 0017), around the perimeter.
  dagster_graphql_urls = {
    for name in keys(local.enabled_stages) :
    name => "http://${local.orchestration_contract.service_name}.${local.stage_namespaces[name]}:${local.orchestration_contract.http_port}/graphql"
  }
  openmetadata_url = local.governance_contract.endpoint
  # The archived-logs route (ADR 0021): read with the key the SeaweedFS
  # credentials Job mints, over F33 (ADR 0017).
  log_archives = {
    for name, prefix in local.stage_log_prefixes :
    name => { base_uri = "${local.artifact_base_uri}/${prefix}", namespace = local.stage_namespaces[name] }
  }
  s3_endpoint_url        = module.seaweedfs.contract.endpoint
  s3_region              = module.seaweedfs.contract.region
  log_reader_secret_name = module.seaweedfs.log_reader_secret_name
}

module "traefik" {
  source = "../../modules/access/traefik"
  count  = local.access_enabled ? 1 : 0

  namespace           = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file    = "${path.root}/../../../helm/values/local/traefik.yaml"
  chart_package_path  = var.traefik_chart_package_path
  routes              = local.ingress_routes
  route_tls           = local.route_tls
  cluster_issuer_name = local.route_tls ? module.cert_manager[0].cluster_issuer_name : null
  group_middlewares   = local.identity_enabled ? module.perimeter[0].group_middlewares : {}
  dynamic_config      = local.identity_enabled ? module.perimeter[0].traefik_dynamic_config : null
  backend_transports  = module.trino.traefik_backends
}

# ADR 0014 seam 3. The proxy discovers the issuer at its public URL when it
# starts, so it waits for the in-cluster resolution and the CA (through
# ca_configmap_name) and for the realm Job (through identity_oidc). The auth
# route's Ingress is not ordered before it: Traefik takes the middlewares from
# this module, so a module-level depends_on would be a cycle, and the proxy
# restarts until the route answers.
module "perimeter" {
  source = "../../modules/access/perimeter"
  count  = local.identity_enabled ? 1 : 0

  namespace         = kubernetes_namespace_v1.shared.metadata[0].name
  base_domain       = local.base_domain
  callback_host     = local.perimeter_callback_host
  portal_url        = "${local.route_scheme}://${local.base_domain}"
  route_scheme      = local.route_scheme
  ca_configmap_name = local.identity_deployed ? module.in_cluster_resolution[0].ca_configmap_name : null
  identity_roles    = local.identity_roles
  role_mapping      = local.identity_oidc.role_mapping
  role_claim        = local.identity_oidc.role_claim
  issuer_url        = local.identity_oidc.issuer_url
  client_id         = local.identity_oidc.clients.perimeter.client_id
  client_secret_ref = local.identity_oidc.clients.perimeter.secret_ref
  cli_client_id     = local.identity_oidc.clients["olf-cli"].client_id
  protected_refs    = local.perimeter_protected_refs
  path_refs         = local.query_api_enabled ? ["shared/query_api"] : []
  scope             = local.identity_scope

  github_organization = try(var.identity_github.organization, null)
}

# Pods reach <scheme>://<service>.<base_domain> through Traefik and, over
# HTTPS, trust the local CA, so a back-channel call sees the same URL (and
# token `iss`) as a browser. ADR 0013.
module "in_cluster_resolution" {
  source = "../../modules/access/in-cluster-resolution"
  count  = local.access_enabled ? 1 : 0

  namespace        = kubernetes_namespace_v1.shared.metadata[0].name
  base_domain      = local.base_domain
  trust_namespaces = values(local.stage_namespaces)
  trust_namespace_uids = concat(
    [kubernetes_namespace_v1.shared.metadata[0].uid],
    [for ns in kubernetes_namespace_v1.stage : ns.metadata[0].uid],
  )

  # Not on Traefik: the CoreDNS rewrite only names its Service, and Superset
  # and Dex mount olf-local-ca, which this module publishes. Waiting on Traefik,
  # which waits on their routes, left a fresh cluster waiting forever.
  depends_on = [module.cert_manager]
}

# ADR 0014 seam 2. `external` deploys nothing: the contract is taken from the
# profile and the operator supplies the client Secrets it references.
module "dex" {
  source = "../../modules/identity/dex"
  count  = local.identity_dex ? 1 : 0

  namespace = kubernetes_namespace_v1.shared.metadata[0].name
  hostname  = "auth.${local.base_domain}"
  scheme    = local.route_scheme
  github    = var.identity_github
  # The fixed name in_cluster_resolution publishes (count-gated, so not referenced).
  ca_configmap_name   = "olf-local-ca"
  clients             = local.identity_client_redirects
  public_clients      = local.identity_public_clients
  postgresql_contract = module.postgresql.contract

  depends_on = [
    module.postgresql,
  ]
}

# Ingress and perimeter became optional (block presence in the profile); keep
# the state of clusters that already have them.
moved {
  from = module.cert_manager
  to   = module.cert_manager[0]
}
moved {
  from = module.portal
  to   = module.portal[0]
}
moved {
  from = module.traefik
  to   = module.traefik[0]
}
moved {
  from = module.perimeter
  to   = module.perimeter[0]
}
moved {
  from = module.in_cluster_resolution
  to   = module.in_cluster_resolution[0]
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
  # People's Dex tokens (ADR 0019): the GitHub connector's id names the one
  # principal every person's token maps to.
  person_access = local.identity_dex ? {
    issuer_url        = local.identity_oidc.issuer_url
    client_id         = local.identity_oidc.clients["olf-cli"].client_id
    principal_name    = module.dex[0].connector_id
    role_mapping      = local.identity_oidc.role_mapping
    data_access       = local.identity_roles.data_access
    ca_configmap_name = module.in_cluster_resolution[0].ca_configmap_name
  } : null
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
    # The pod mounts the CA ConfigMap this module publishes.
    module.in_cluster_resolution,
  ]
}

module "trino" {
  source = "../../modules/query/trino"

  namespace                  = kubernetes_namespace_v1.shared.metadata[0].name
  base_values_file           = "${path.root}/../../../helm/values/local/trino.yaml"
  chart_package_path         = var.trino_chart_package_path
  query_sizing               = var.query_sizing
  storage_contract           = local.storage_contract
  catalog_contract           = local.catalog_contract
  stage_catalog_contracts    = local.stage_catalog_contracts
  catalog_bootstrap_revision = local.polaris_bootstrap_hash

  stage_namespaces        = local.stage_namespaces
  workload_authentication = local.trino_workload_authentication
  tls_issuer_name         = try(module.cert_manager[0].cluster_issuer_name, null)
  # Trino's OAuth2 UI sign-in needs HTTPS (#425), so a plain-HTTP route shows no
  # login form: the UI is a fixed user and the perimeter (or no auth) gates it.
  web_ui_fixed_user = local.route_tls ? null : "olf-web"
  # Trino refuses OAuth2 on a request that is not secure, so a plain-HTTP route
  # (`issuer: none`) keeps the UI's own login. Dex only: the module sets Dex's
  # endpoints rather than discovering them (modules/query/trino/oidc.tf).
  web_ui_oidc = local.identity_dex && local.route_tls && var.trino_workload_authentication != "off" ? {
    issuer_url = local.identity_oidc.issuer_url
    client_id  = local.identity_oidc.clients.trino.client_id
    secret_ref = local.identity_oidc.clients.trino.secret_ref
  } : null
  # People's queries (ADR 0019): Dex tokens issued to olf-cli, and a people
  # catalog per stage on the credential the Polaris bootstrap created for it.
  people_access = local.identity_dex && local.trino_workload_authentication == "password" ? {
    issuer_url = local.identity_oidc.issuer_url
    client_id  = local.identity_oidc.clients["olf-cli"].client_id
    # Dex's own Service, not the ingress: Traefik takes Trino's route, so it
    # is created after Trino, which needs the keys to start (flow F31).
    keys_url = "http://${one(module.dex[*].service_name)}.${kubernetes_namespace_v1.shared.metadata[0].name}.svc.cluster.local:${one(module.dex[*].http_port)}/keys"
    catalogs = module.polaris.people_credentials_secret_names
  } : null

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
  # One Iceberg connection crawls one catalog: the canonical stage's. Every
  # other governed stage still gets its own database root and services below,
  # which is what `olf infra openmetadata deploy-metadata` seeds into (#131).
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
  # come into existence in Phase 2. `olf infra openmetadata deploy-metadata` creates
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
  # Only with an issuer deployed (spec.identity); without one OpenMetadata keeps
  # its basic login. The client Secret is in this namespace, made by the issuer.
  oidc = !local.identity_deployed ? null : {
    issuer_url        = local.identity_oidc.issuer_url
    scope             = local.identity_scope
    client_id         = local.identity_oidc.clients.openmetadata.client_id
    secret_ref        = local.identity_oidc.clients.openmetadata.secret_ref
    base_url          = "${local.route_scheme}://openmetadata.${local.base_domain}"
    ca_configmap_name = "olf-local-ca" # the fixed name in_cluster_resolution publishes
  }
  stages = {
    for name in keys(local.governed_stages) : name => {
      catalog_database_name  = local.stage_catalog_contracts[name].catalog_name
      pipeline_service_name  = local.stage_databases["dagster_${name}"].db_name
      dagster_webserver_url  = "http://${local.orchestration_contract.service_name}.${local.stage_namespaces[name]}:${local.orchestration_contract.http_port}"
      dashboard_service_name = contains(keys(local.analytics_stages), name) ? local.stage_databases["superset_${name}"].db_name : null
      superset_url           = contains(keys(local.analytics_stages), name) ? "http://${module.superset[name].contract.service_name}.${local.stage_namespaces[name]}:${module.superset[name].contract.http_port}" : null
      superset_namespace     = contains(keys(local.analytics_stages), name) ? local.stage_namespaces[name] : null
    }
  }
  # Read from module.trino, not the contract, so the bootstrap Job that runs
  # the first catalog crawl through Trino is created only once Trino is ready.
  trino_lineage_namespace = "trino://${module.trino.service_name}.${var.shared_namespace}:${module.trino.http_port}"
  # dbt connects on the HTTPS port when workload authentication is on, and
  # names its lineage datasets by that port.
  trino_tls_lineage_namespace = module.trino.tls_port == null ? null : "trino://${module.trino.service_name}.${var.shared_namespace}:${module.trino.tls_port}"

  depends_on = [
    module.polaris,
    module.postgresql,
    module.seaweedfs,
    kubernetes_namespace_v1.stage,
    module.in_cluster_resolution, # publishes olf-local-ca, which the server mounts with SSO on
  ]
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
  # The literal name, as in the query contract: see contracts.tf.
  trino_ca_configmap_name = module.trino.tls_port == null ? null : "olf-local-ca"
  # Only with an issuer deployed (spec.identity); without one Superset keeps
  # its local login. Nothing here names an issuer product.
  bootstrap_job_image = yamldecode(file("${path.root}/../../../../release/component-catalog.yaml")).components.images.k8s_bootstrap
  public_admin        = !local.identity_enabled
  oidc = !local.identity_deployed ? null : {
    issuer_url        = local.identity_oidc.issuer_url
    role_claim        = local.identity_oidc.role_claim
    role_mapping      = local.identity_oidc.role_mapping
    grants            = local.identity_roles.grants.reporting
    sign_out_url      = local.sign_out_url
    scope             = local.identity_scope
    client_id         = local.identity_oidc.clients.superset.client_id
    secret_ref        = local.identity_oidc.clients.superset.secret_ref
    secret_namespace  = kubernetes_namespace_v1.shared.metadata[0].name
    ca_configmap_name = "olf-local-ca" # the fixed name in_cluster_resolution publishes
  }

  depends_on = [
    module.postgresql,
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
    tls_port                   = module.trino.tls_port
    # A fixed name like the CA ConfigMap; the credential Job writes it to every stage namespace.
    credential_secret_name = module.trino.credential_secret_name
    # The fixed name in_cluster_resolution publishes (count-gated, so not referenced).
    ca_configmap_name = module.trino.tls_port == null ? null : "olf-local-ca"
  })
  postgresql_contract   = local.stage_metadata_database_contracts[each.key]
  code_locations        = local.orchestration_contract.code_locations
  artifact_bucket_name  = local.artifact_bucket_contract.bucket_name
  artifact_base_uri     = "${local.artifact_bucket_contract.artifact_base_uri}/activations/${each.key}"
  log_base_uri          = "${local.artifact_bucket_contract.artifact_base_uri}/${local.stage_log_prefixes[each.key]}"
  run_artifact_base_uri = "${local.artifact_bucket_contract.artifact_base_uri}/activations/${each.key}/run-artifacts"

  depends_on = [
    module.trino,
    module.openmetadata,
    module.postgresql,
    module.superset,
  ]
}

# F20: the policy sees the post-DNAT endpoint address, and whether it also
# sees the Service address depends on the CNI, so both are allowed.
data "kubernetes_service_v1" "apiserver" {
  metadata {
    name      = "kubernetes"
    namespace = "default"
  }
}

data "kubernetes_endpoints_v1" "apiserver" {
  metadata {
    name      = "kubernetes"
    namespace = "default"
  }
}

data "kubernetes_nodes" "all" {}

locals {
  network_flows = yamldecode(file("${path.root}/../../../../release/network-flows.yaml"))

  # A workload's policy is rendered only if the workload is deployed, so a
  # profile without Dex or governance plans no policy for them. Names
  # not listed are always deployed.
  workload_deployed = merge(
    { for name in ["traefik", "portal", "publish-local-ca"] : name => local.access_enabled },
    { for name in ["cert-manager-controller", "cert-manager-cainjector", "cert-manager-webhook", "cert-manager-startupapicheck"] : name => length(module.cert_manager) > 0 },
    { for name in ["oauth2-proxy", "perimeter-cookie-secret"] : name => local.identity_enabled },
    { "platform-api" = length(module.platform_api) > 0 },
    { for name in ["dex", "dex-credentials"] : name => local.identity_dex },
    { for name in ["openmetadata", "opensearch", "governance-bootstrap", "governance-credential-replication", "openmetadata-trino-ingestion"] : name => local.governance_enabled },
  )

  # ipBlock cannot name a host, so profile-driven egress is any address on the
  # port the profile names (F23).
  profile_flows = concat(
    var.identity_issuer == "external" ? [{
      id = "F23"
      from = [
        { scope = "shared", pods = { "openlakeforge.io/workload" = "oauth2-proxy" } },
        { scope = "shared", pods = { "app.kubernetes.io/name" = "platform-api" } },
      ]
      to    = { scope = "any" }
      ports = [try(tonumber(regex("^(?i:https)://(?:\\[[^]]*\\]|[^/:?#]+):([0-9]+)", var.identity_external.issuer_url)[0]), 443)] # the issuer URL may name a port
      tag   = "T"
    }] : [],
  )
}

module "network_policy" {
  source = "../../modules/security/network-policy"

  stage_namespaces = { for name, namespace in kubernetes_namespace_v1.stage : name => namespace.metadata[0].name }
  shared_namespace = kubernetes_namespace_v1.shared.metadata[0].name
  flows            = concat(local.network_flows.flows, local.profile_flows)
  workloads        = { for name, labels in local.network_flows.workloads : name => labels if lookup(local.workload_deployed, name, true) }
  node_cidrs = [
    for node in data.kubernetes_nodes.all.nodes : "${one([for a in node.status[0].addresses : a.address if a.type == "InternalIP"])}/32"
  ]
  apiserver_cidrs = [
    for ip in concat(
      [data.kubernetes_service_v1.apiserver.spec[0].cluster_ip],
      flatten([for subset in data.kubernetes_endpoints_v1.apiserver.subset : [for address in subset.address : address.ip]]),
    ) : "${ip}/32"
  ]
}
