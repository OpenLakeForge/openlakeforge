# #799: olf-cli trusts the perimeter, so the perimeter's login can ask for a
# token whose audience is olf-cli. Nobody else gets trustedPeers.

mock_provider "kubernetes" {}

variables {
  namespace = "olf-system"
  hostname  = "auth.olf.localhost"
  github = {
    organization         = "acme"
    teams                = { viewer = ["viewers"] }
    oauth_app_secret_ref = { name = "gh", client_id_key = "id", client_secret_key = "secret" }
  }
  clients = {
    perimeter = { redirect_uris = ["http://oauth2.olf.localhost/oauth2/callback"] }
    superset  = { redirect_uris = [] }
  }
  public_clients = ["olf-cli"]
  postgresql_contract = {
    host      = "pg"
    port      = 5432
    databases = { dex = { db_name = "dex", db_user = "dex", credentials_secret_name = "dex-db" } }
  }
}

run "the_perimeter_is_a_trusted_peer_of_olf_cli_only" {
  command = plan

  variables {
    trusted_peers = { "olf-cli" = ["perimeter"] }
  }

  assert {
    condition     = [for c in jsondecode(kubernetes_config_map_v1.dex.data["config.yaml"]).staticClients : c.id if try(c.trustedPeers, []) == ["perimeter"]] == ["olf-cli"]
    error_message = "olf-cli alone must list the perimeter in trustedPeers"
  }
}

run "no_peers_renders_no_trustedpeers" {
  command = plan

  assert {
    condition     = alltrue([for c in jsondecode(kubernetes_config_map_v1.dex.data["config.yaml"]).staticClients : !contains(keys(c), "trustedPeers")])
    error_message = "a client with no peers must carry no trustedPeers field"
  }
}

run "a_peer_that_is_not_a_client_is_refused" {
  command = plan

  variables {
    trusted_peers = { "olf-cli" = ["nobody"] }
  }

  expect_failures = [var.trusted_peers]
}

run "a_github_enterprise_host_needs_the_published_ca" {
  command = plan

  variables {
    github = {
      organization         = "acme"
      host                 = "https://ghe.example.com"
      teams                = { viewer = ["viewers"] }
      oauth_app_secret_ref = { name = "gh", client_id_key = "id", client_secret_key = "secret" }
    }
    ca_configmap_name = null
  }

  expect_failures = [var.ca_configmap_name]
}
