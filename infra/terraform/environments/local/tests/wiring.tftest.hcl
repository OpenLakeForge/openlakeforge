# How the local root wires identity and per-person queries (#799), asserted on the
# planned values the modules receive rather than on the root's source. Nothing
# here needs a cluster: providers are mocked and the three cluster lookups and the
# foundation state are overridden.

mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "random" {}

override_data {
  target = data.terraform_remote_state.local_foundation
  values = {
    outputs = {
      foundation_contract = {
        provider            = "local"
        implementation      = "kind"
        cluster_name        = "test"
        kube_context        = "kind-test"
        kubeconfig_path     = "/nonexistent/kubeconfig"
        cluster_config_path = "/nonexistent/kind.yaml"
      }
    }
  }
}

override_data {
  target = data.kubernetes_service_v1.apiserver
  values = { spec = [{ cluster_ip = "10.96.0.1" }] }
}

override_data {
  target = data.kubernetes_endpoints_v1.apiserver
  values = { subset = [{ address = [{ ip = "172.18.0.2" }] }] }
}

override_data {
  target = data.kubernetes_nodes.all
  values = { nodes = [{ status = [{ addresses = [{ type = "InternalIP", address = "172.18.0.2" }] }] }] }
}

variables {
  stages = {
    dev  = { enabled = true, analytics = true, governance = false }
    prod = { enabled = true, analytics = true, governance = false }
  }
  bucket_names = {
    dev  = { bronze = "olf-dev-bronze", silver = "olf-dev-silver", gold = "olf-dev-gold" }
    prod = { bronze = "olf-prod-bronze", silver = "olf-prod-silver", gold = "olf-prod-gold" }
  }
  access_base_domain = "olf.localhost"
  platform_api_image = "platform-api:test"
  ops_bucket_name    = "olf-ops"
  identity_issuer    = "github"
  identity_github = {
    organization = "acme"
    teams        = { platform-admin = ["admins"], data-engineer = ["eng"], analyst = ["an"], viewer = ["all"] }
    oauth_app_secret_ref = {
      name              = "github-oauth"
      client_id_key     = "id"
      client_secret_key = "secret"
    }
  }
}

run "dex_serves_person_queries" {
  command = plan

  variables {
    access_tls = "local-ca"
  }

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).person_query == true
    error_message = "person_query must be on with Dex"
  }

  assert {
    condition     = keys(jsondecode(module.platform_api[0].settings_json).query_endpoints) == ["dev", "prod"]
    error_message = "query_endpoints must name exactly the stages with a person query route"
  }

  assert {
    condition = alltrue([
      for stage, endpoint in jsondecode(module.platform_api[0].settings_json).query_endpoints :
      endpoint.catalog == "lakehouse_${stage}" && endpoint.statement_url == "http://trino.olf-${stage}.svc.cluster.local:8080/v1/statement"
    ])
    error_message = "each endpoint names its stage's lakehouse_<stage> catalog and its own coordinator's plain HTTP URL: Trino has no authentication of its own"
  }

  assert {
    condition     = keys(module.trino) == ["dev", "prod"]
    error_message = "one Trino coordinator per enabled stage"
  }

  assert {
    condition = alltrue([
      for stage in ["dev", "prod"] :
      local.provider_contracts.stages[stage].query.service_ref == "stage/${stage}/query" && local.provider_contracts.stages[stage].query.endpoint == "http://trino.olf-${stage}:8080"
    ])
    error_message = "each stage's query binding names its own coordinator in its own namespace"
  }

  assert {
    condition     = [for ref in keys(output.route_groups) : ref if endswith(ref, "/query") || endswith(ref, "/query_api")] == ["stage/dev/query", "stage/dev/query_api", "stage/prod/query", "stage/prod/query_api"]
    error_message = "Trino is routed per stage, both the host and its /v1 path"
  }

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).public_origin == "https://olf.localhost"
    error_message = "public_origin must carry the route scheme and the base domain"
  }

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).audiences == ["olf-cli"]
    error_message = "the API accepts olf-cli's audience alone, never the perimeter's"
  }

  assert {
    condition     = module.perimeter[0].login_audience == "olf-cli"
    error_message = "the perimeter requests olf-cli's audience with Dex"
  }

  assert {
    condition     = jsonencode(module.dex[0].public_client_trusted_peers) == jsonencode({ "olf-cli" = ["perimeter", "superset"] })
    error_message = "Dex lets the perimeter and Superset obtain tokens for olf-cli"
  }

  assert {
    condition = alltrue([for stage in keys(var.stages) :
      contains(module.dex[0].client_redirect_uris["superset"], "https://superset-${stage}.olf.localhost/api/v1/database/oauth2/")
    ])
    error_message = "Dex must accept Superset's database OAuth2 redirect on every analytics stage"
  }

  assert {
    condition     = [for ref, group in output.route_groups : ref if group == module.perimeter[0].api_group] == ["platform-api"]
    error_message = "only the platform-api route uses the API middleware group"
  }
}

run "external_issuer_leaves_person_queries_off" {
  command = plan

  variables {
    identity_issuer = "external"
    identity_external = {
      issuer_url   = "https://issuer.example.com"
      role_claim   = "groups"
      role_mapping = { platform-admin = ["admins"], data-engineer = ["eng"], analyst = ["an"], viewer = ["all"] }
    }
  }

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).person_query == false
    error_message = "person_query must be off without Dex"
  }

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).query_endpoints == {}
    error_message = "no query endpoints without person queries"
  }

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).audiences == ["olf-cli"]
    error_message = "the API accepts olf-cli's audience alone"
  }

  assert {
    condition     = module.perimeter[0].login_audience == null
    error_message = "no login audience without Dex"
  }

  assert {
    condition     = length(module.dex) == 0
    error_message = "an external issuer deploys no Dex"
  }

  assert {
    condition     = [for ref, group in output.route_groups : ref if group == module.perimeter[0].api_group] == ["platform-api"]
    error_message = "only the platform-api route uses the API middleware group"
  }
}

run "plain_http_without_identity_has_no_api" {
  command = plan

  variables {
    identity_issuer = null
    access_tls      = "none"
  }

  assert {
    condition     = length(module.platform_api) == 0 && length(module.perimeter) == 0
    error_message = "no identity deploys neither the API nor the perimeter"
  }
}

run "portal_image_override_reaches_the_portal" {
  command = plan

  variables {
    portal_image = "ghcr.io/openlakeforge/openlakeforge-portal:pr-1@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }

  assert {
    condition     = module.portal[0].image == var.portal_image
    error_message = "portal_image must reach the portal module"
  }
}

run "portal_keeps_its_pin_when_no_image_is_given" {
  command = plan

  assert {
    condition     = can(regex("^ghcr.io/openlakeforge/openlakeforge-portal:[^@]+@sha256:[0-9a-f]{64}$", module.portal[0].image))
    error_message = "an unset portal_image must leave the module's digest-pinned default"
  }
}

run "portal_image_must_be_pinned_by_digest" {
  command = plan

  variables {
    portal_image = "ghcr.io/openlakeforge/openlakeforge-portal:pr-1"
  }

  expect_failures = [var.portal_image]
}

run "the_default_issuer_serves_plain_http" {
  command = plan

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).public_origin == "http://olf.localhost"
    error_message = "the default issuer is none: routes, and the origin the portal reports, are plain HTTP"
  }
}

run "a_single_stage_deployment_renders_exactly_one_coordinator" {
  command = plan

  variables {
    stages = {
      dev = { enabled = true, analytics = true, governance = false }
    }
    bucket_names = {
      dev = { bronze = "olf-dev-bronze", silver = "olf-dev-silver", gold = "olf-dev-gold" }
    }
  }

  assert {
    condition     = keys(module.trino) == ["dev"]
    error_message = "Slim deploys one coordinator, in olf-dev"
  }
}

# #1156: the probe grant and its caller are wired together, from the namespace
# resources. probe_namespaces is built from the resources' own attributes (the
# reference the network policy and the Superset service users use too), so
# Terraform creates each Role after its namespace.
run "service_probes_are_wired_to_each_stage_namespace" {
  command = plan

  variables {
    access_tls = "local-ca"
  }

  assert {
    condition = jsondecode(module.platform_api[0].settings_json).stage_namespaces == {
      dev  = "olf-dev"
      prod = "olf-prod"
    }
    error_message = "the API's settings must name each enabled stage's namespace"
  }

  assert {
    condition = jsondecode(module.platform_api[0].settings_json).trino_urls == {
      dev  = "http://trino.olf-dev.svc.cluster.local:8080"
      prod = "http://trino.olf-prod.svc.cluster.local:8080"
    }
    error_message = "the probe needs each coordinator's base URL, not its statement URL"
  }

  assert {
    condition     = jsondecode(module.platform_api[0].settings_json).polaris_health_url == "http://polaris-mgmt.olf-system.svc:8182"
    error_message = "the probe reads Polaris's management port, not the catalog's REST port"
  }

  assert {
    condition     = length(terraform_data.platform_api_probe_grant) == 1
    error_message = "the F20 egress is static, so the root must check the probe grant accompanies the platform API"
  }
}

# ADR 0007: Slim is a strict no-op for quality publication. No CronJob, key,
# Secret or policy exists unless a stage has governance.
run "slim_renders_no_quality_reconciler" {
  command = plan

  assert {
    condition     = length(module.quality_reconciler.cron_job_names) == 0 && module.quality_reconciler.service_account_name == null
    error_message = "a deployment without governance must render no reconciler CronJob or ServiceAccount"
  }

  assert {
    condition     = length(module.seaweedfs.quality_sync_secret_names) == 0
    error_message = "a deployment without governance must mint no quality sync key"
  }

  assert {
    condition     = !contains(keys(module.network_policy.workload_policy_names), "quality-reconciler")
    error_message = "a deployment without governance must render no reconciler NetworkPolicy"
  }
}

# Each governed stage gets its own pass, key and prefix, so the DEV reconciler
# holds nothing that reaches PROD evidence or PROD tables.
run "a_governed_stage_gets_a_reconciler_pinned_to_it" {
  command = plan

  variables {
    stages = {
      dev  = { enabled = true, analytics = true, governance = true }
      prod = { enabled = true, analytics = true, governance = false }
    }
  }

  assert {
    condition     = module.quality_reconciler.cron_job_names == { dev = "quality-reconciler-dev" }
    error_message = "only the governed stage has a reconciler"
  }

  assert {
    condition     = module.seaweedfs.quality_sync_secret_names == { dev = "quality-reconciler-dev-s3-creds" }
    error_message = "the reconciler's key is the stage's own"
  }

  assert {
    condition     = contains(keys(module.network_policy.workload_policy_names), "quality-reconciler")
    error_message = "the reconciler's pods are isolated by a NetworkPolicy of their own"
  }
}

# ADR 0024: without metrics there is no Prometheus, no endpoint in the
# contract and no flow row for it on either side.
run "a_profile_without_metrics_renders_no_prometheus" {
  command = plan

  assert {
    condition     = length(module.metrics) == 0 && !contains(keys(module.network_policy.workload_policy_names), "prometheus")
    error_message = "metrics off must render no Prometheus resource or policy"
  }

  assert {
    condition     = length([for f in local.policy_flows : f.id if contains(["F45", "F46", "F47", "F48", "F49"], f.id)]) == 0
    error_message = "metrics off must leave no Prometheus row, including the stage ingress (F48) and the API's egress (F45)"
  }

  assert {
    condition = (
      output.provider_contracts.shared.observability.metrics.supported
      && output.provider_contracts.shared.observability.metrics.query_api == null
      && output.provider_contracts.shared.observability.metrics.query_url == null
    )
    error_message = "the local provider supports metrics, but publishes no endpoint for a backend it did not deploy"
  }
}

run "a_profile_with_metrics_deploys_prometheus_and_publishes_it" {
  command = plan

  variables {
    observability = { metrics_enabled = true, metrics_retention_days = 14 }
  }

  assert {
    condition     = length(module.metrics) == 1 && contains(keys(module.network_policy.workload_policy_names), "prometheus")
    error_message = "metrics on deploys Prometheus with a NetworkPolicy of its own"
  }

  assert {
    condition     = toset([for f in local.policy_flows : f.id if contains(["F45", "F46", "F47", "F48", "F49"], f.id)]) == toset(["F45", "F46", "F47", "F48", "F49"])
    error_message = "metrics on renders F45 to F49"
  }

  assert {
    condition = (
      output.provider_contracts.shared.observability.metrics.query_api == "prometheus-http-v1"
      && output.provider_contracts.shared.observability.metrics.query_url == "http://prometheus.olf-system.svc:9090"
      && output.provider_contracts.shared.observability.metrics.retention_supported
    )
    error_message = "the contract names the Prometheus Service, and retention is enforced"
  }
}
