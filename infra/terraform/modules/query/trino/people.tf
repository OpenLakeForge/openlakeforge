# People's queries (ADR 0019, build step 5). A person's Dex token, issued to
# olf-cli, authenticates them by e-mail; their queries go only to the people
# catalogs, one per stage, which pass the token on to Polaris (session=USER)
# and read files only with the credentials Polaris vends for each table. Who
# may read or write what is Polaris's decision, not a rule here.
locals {
  people_enabled = var.people_access != null
  people_https   = startswith(try(var.people_access.keys_url, ""), "https://")

  people_jwt_properties = !local.people_enabled ? [] : concat(
    [
      # Trino fetches the keys at startup and does not start without them, so
      # they come from the issuer's in-cluster address: the public one is
      # served by Traefik, which is created after Trino (ADR 0016, "Issuer
      # reachability"). The issuer the tokens name is still the public one.
      "http-server.authentication.jwt.key-file=${var.people_access.keys_url}",
      "http-server.authentication.jwt.required-issuer=${var.people_access.issuer_url}",
      # A token Dex issued to the perimeter, Superset, OpenMetadata or the
      # Trino UI client names that client as its audience and is refused.
      "http-server.authentication.jwt.required-audience=${var.people_access.client_id}",
      "http-server.authentication.jwt.principal-field=email",
    ],
    # The key requests use their own HTTP client (prefix `jwk`), separate from
    # the internal-communication and Web UI ones.
    local.people_https ? ["jwk.http-client.trust-store-path=/etc/trino/tls/ca.crt"] : [],
  )

  # The stage's keyless catalog (main.tf) under the stage's name. Two names
  # only because Trino's rules still keep e-mail users and service users on
  # separate catalogs until its authentication goes (#983).
  people_catalog_properties = {
    for stage in try(var.people_access.stages, []) : stage => local.rest_catalog_properties[stage]
  }

  # A person, whose user is an e-mail, may use the people catalogs and nothing
  # else; service principals never reach them.
  people_catalog_rules = [
    for name in compact([join("|", try(var.people_access.stages, []))]) : {
      user = ".*@.*", catalog = "(${name})", allow = "all"
    }
  ]
  # A token's principal is its e-mail, and it may act only as that user.
  people_principal_rules = [
    for enabled in [local.people_enabled] : { principal = "(.*@.*)", principal_to_user = "$1", allow = true } if enabled
  ]
  # A person may run queries and see and kill only their own: Trino always
  # lets a user view and kill their own queries. Service principals keep what
  # they had with no `queries` section, every action on every query.
  people_query_rules = {
    for key, rules in {
      queries = [
        { user = ".*@.*", allow = ["execute"] },
        { allow = ["execute", "kill", "view"] },
      ]
    } : key => rules if local.people_enabled
  }
}
