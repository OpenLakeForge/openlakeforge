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
  stages       = []
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
