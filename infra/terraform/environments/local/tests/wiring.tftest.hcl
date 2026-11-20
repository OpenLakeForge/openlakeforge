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

# ADR 0021, decision 8: without identity the API serves every caller anonymously, behind no perimeter.
run "plain_http_without_identity_serves_the_api_anonymously" {
  command = plan

  variables {
    identity_issuer = null
    access_tls      = "none"
  }

  assert {
    condition     = length(module.platform_api) == 1 && length(module.perimeter) == 0
    error_message = "no identity deploys the API and no perimeter"
  }

  assert {
    condition = (
      jsondecode(module.platform_api[0].settings_json).anonymous_role == "platform-admin"
      && jsondecode(module.platform_api[0].settings_json).issuer_url == null
      && jsondecode(module.platform_api[0].settings_json).openmetadata_url == null
      && jsondecode(module.platform_api[0].settings_json).public_origin == "http://olf.localhost"
      && jsondecode(module.platform_api[0].settings_json).person_query == true
      && length(jsondecode(module.platform_api[0].settings_json).query_endpoints) > 0
    )
    error_message = "the anonymous API has the highest role, no issuer, no OpenMetadata, the plain-HTTP origin and queries"
  }

  assert {
    condition     = output.route_groups["platform-api"] == ""
    error_message = "the anonymous API's route sits behind no perimeter group"
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

# ADR 0024: without metrics there is no Prometheus or kube-state-metrics, no
# endpoint in the contract and no flow row for either on either side.
run "a_profile_without_metrics_renders_no_prometheus" {
  command = plan

  assert {
    condition = length(module.metrics) == 0 && length(setintersection(
      toset(keys(module.network_policy.workload_policy_names)), toset(["prometheus", "kube-state-metrics"])
    )) == 0
    error_message = "metrics off must render no Prometheus or kube-state-metrics resource or policy"
  }

  assert {
    condition     = length([for f in local.policy_flows : f.id if contains(["F45", "F46", "F47", "F48", "F49", "F50", "F51"], f.id)]) == 0
    error_message = "metrics off must leave no metrics row, including the stage ingress (F48) and the API's egress (F45)"
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
    condition = length(module.metrics) == 1 && length(setintersection(
      toset(keys(module.network_policy.workload_policy_names)), toset(["prometheus", "kube-state-metrics"])
    )) == 2
    error_message = "metrics on deploys Prometheus and kube-state-metrics, each with a NetworkPolicy of its own"
  }

  assert {
    condition     = toset([for f in local.policy_flows : f.id if contains(["F45", "F46", "F47", "F48", "F49", "F50", "F51"], f.id)]) == toset(["F45", "F46", "F47", "F48", "F49", "F50", "F51"])
    error_message = "metrics on renders F45 to F51"
  }

  assert {
    condition     = module.metrics[0].kube_state_metrics_role_namespaces == sort(concat(["olf-system"], [for name, namespace in kubernetes_namespace_v1.stage : namespace.metadata[0].name]))
    error_message = "kube-state-metrics holds a Role in olf-system and in each enabled stage namespace, and in no other"
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

# #1327: the API states the archive retention the contract reports as applied.
run "the_log_archive_retention_reaches_the_api" {
  command = plan

  variables {
    observability = { archive_retention_days = 14 }
  }

  assert {
    condition = (
      output.provider_contracts.shared.observability.logs.archive.retention_supported
      && jsondecode(module.platform_api[0].settings_json).log_archive_retention_days == 14
    )
    error_message = "the platform API must state the archive retention the local provider applies"
  }
}

# #794: notifications and the run quality answer link the alerts guide at this release's tag unless overridden.
run "the_runbook_defaults_to_the_alerts_guide_at_the_release_tag" {
  command = plan

  variables {
    observability          = { alert_receivers = [{ name = "on-call", type = "webhook" }] }
    alert_receiver_secrets = { on-call = { secret_name = "alert-webhooks", key = "webhook-url" } }
  }

  assert {
    condition = (
      jsondecode(module.alerts.settings_json).links.runbook_base_url == "https://github.com/OpenLakeForge/openlakeforge/blob/v${yamldecode(file("../../../../release/component-catalog.yaml")).distribution.version}/docs/guides/operations/alerts.md"
      && jsondecode(module.platform_api[0].settings_json).runbook_base_url == jsondecode(module.alerts.settings_json).links.runbook_base_url
    )
    error_message = "the evaluator and the API must link the alerts guide at the distribution's release tag"
  }
}

run "an_overridden_runbook_reaches_the_evaluator_and_the_api" {
  command = plan

  variables {
    observability          = { alert_receivers = [{ name = "on-call", type = "webhook" }] }
    alert_receiver_secrets = { on-call = { secret_name = "alert-webhooks", key = "webhook-url" } }
    runbook_base_url       = "https://docs.example.test/runbooks/alerts"
  }

  assert {
    condition = (
      jsondecode(module.alerts.settings_json).links.runbook_base_url == "https://docs.example.test/runbooks/alerts"
      && jsondecode(module.platform_api[0].settings_json).runbook_base_url == "https://docs.example.test/runbooks/alerts"
    )
    error_message = "an overridden runbook page must reach the evaluator and the API"
  }
}

run "a_runbook_override_with_a_fragment_is_refused" {
  command = plan

  variables {
    runbook_base_url = "https://docs.example.test/alerts.md#quality"
  }

  expect_failures = [var.runbook_base_url]
}
