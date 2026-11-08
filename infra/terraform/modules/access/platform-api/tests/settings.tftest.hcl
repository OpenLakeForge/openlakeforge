# #799: the API's CSRF origin and the per-person query switch reach its settings.

mock_provider "kubernetes" {}

variables {
  namespace    = "olf-system"
  image        = "platform-api:test"
  issuer_url   = "http://auth.olf.localhost"
  audiences    = ["olf-cli"]
  role_claim   = "groups"
  role_mapping = { viewer = ["g"] }
  precedence   = ["viewer"]
  stages       = ["dev"]

  bootstrap_job_image = "bootstrap:test"
  query_endpoints = {
    dev = { statement_url = "https://trino.olf-system.svc.cluster.local:8443/v1/statement", catalog = "dev" }
  }
}

run "defaults_refuse_cross_origin_writes_and_queries" {
  command = plan

  assert {
    condition     = jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).person_query == false
    error_message = "person_query must default to false"
  }

  assert {
    condition     = jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).public_origin == null
    error_message = "public_origin must default to unset"
  }
}

run "origin_and_query_switch_are_rendered" {
  command = plan

  variables {
    public_origin = "http://olf.localhost"
    person_query  = true
  }

  assert {
    condition = (
      jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).public_origin == "http://olf.localhost"
      && jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).person_query == true
    )
    error_message = "public_origin and person_query must reach settings.json"
  }
}

# #800: the query endpoints and the signing key follow person_query alone.
run "query_endpoints_and_signing_key_are_absent_without_person_query" {
  command = plan

  assert {
    condition     = jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).query_endpoints == {}
    error_message = "query_endpoints must not be rendered while person_query is false"
  }

  assert {
    condition     = length(kubernetes_deployment_v1.platform_api.spec[0].template[0].spec[0].container[0].env) == 0
    error_message = "no signing key may be mounted while person_query is false"
  }

  assert {
    condition     = length(kubernetes_job_v1.query_signing_key) == 0
    error_message = "no signing key may be minted while person_query is false"
  }
}

run "query_endpoints_and_signing_key_are_wired_with_person_query" {
  command = plan

  variables {
    public_origin = "http://olf.localhost"
    person_query  = true
  }

  assert {
    condition = (
      jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).query_endpoints.dev.catalog == "dev"
      && startswith(jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).query_endpoints.dev.statement_url, "https://trino.")
    )
    error_message = "each stage's statement URL and catalog must reach settings.json"
  }

  assert {
    condition = [
      for env in kubernetes_deployment_v1.platform_api.spec[0].template[0].spec[0].container[0].env :
      env.value_from[0].secret_key_ref[0].name if env.name == "OPENLAKEFORGE_QUERY_SIGNING_KEY"
    ] == ["platform-api-query-signing-key"]
    error_message = "the signing key must be mounted once, from the Secret the Job mints"
  }

  # The value is minted in-cluster; neither the settings, which land in a
  # ConfigMap, nor the module's outputs may name it.
  assert {
    condition     = !strcontains(kubernetes_config_map_v1.settings.data["settings.json"], "signing")
    error_message = "the settings file must not mention the signing key"
  }

  assert {
    condition     = !strcontains(jsonencode([output.service_name, output.service_port, output.path]), "signing")
    error_message = "no output may carry the signing key"
  }
}

# #991: keys come from the issuer's Service when one is given.
run "internal_issuer_url_sets_the_jwks_url" {
  command = plan

  variables {
    internal_issuer_url = "http://dex.olf-system.svc.cluster.local:5556"
  }

  assert {
    condition     = jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).jwks_url == "http://dex.olf-system.svc.cluster.local:5556/keys"
    error_message = "jwks_url must be the internal URL's /keys"
  }
}

run "no_internal_issuer_url_keeps_discovery" {
  command = plan

  assert {
    condition     = jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).jwks_url == null
    error_message = "jwks_url must be unset without an internal issuer URL"
  }
}
