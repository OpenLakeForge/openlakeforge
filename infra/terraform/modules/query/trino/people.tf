# People's queries (ADR 0019, build step 5). A person's Dex token, issued to
# olf-cli, authenticates them by e-mail; their queries go only to the people
# catalogs, one per stage, which pass the token on to Polaris (session=USER)
# and read files only with the credentials Polaris vends for each table. Who
# may read or write what is Polaris's decision, not a rule here.
locals {
  people_enabled = var.people_access != null
  people_https   = startswith(try(var.people_access.issuer_url, ""), "https://")

  people_jwt_properties = !local.people_enabled ? [] : concat(
    [
      "http-server.authentication.jwt.key-file=${var.people_access.issuer_url}/keys",
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

  people_catalog_env = flatten([
    for stage, secret in try(var.people_access.catalogs, {}) : [
      for field in ["ID", "SECRET"] : {
        name      = "OPENLAKEFORGE_PEOPLE_${upper(stage)}_CLIENT_${field}"
        valueFrom = { secretKeyRef = { name = secret, key = "POLARIS_CLIENT_${field}" } }
      }
    ]
  ])

  # The workload catalogs' storage lines without their key.
  people_s3_properties = join("\n", compact([
    "fs.native-s3.enabled=true",
    var.storage_contract.endpoint == null ? "" : "s3.endpoint=${var.storage_contract.endpoint}",
    var.storage_contract.path_style_access == null ? "" : "s3.path-style-access=${var.storage_contract.path_style_access}",
    "s3.region=${var.storage_contract.region}",
  ]))

  # The catalog's own credential, trino-people-<stage>, holds no grant: Trino
  # uses it only for the catalog's configuration call. Every table request
  # carries the person's token (the `token` extra credential), and a request
  # without it falls through to a token exchange Polaris refuses.
  people_catalog_properties = {
    for stage, secret in try(var.people_access.catalogs, {}) : stage => <<-CATALOG
      connector.name=iceberg
      iceberg.catalog.type=rest
      iceberg.rest-catalog.uri=${try(local.stage_catalog_contracts[stage].rest_uri, "")}
      iceberg.rest-catalog.warehouse=${try(local.stage_catalog_contracts[stage].warehouse, "")}
      iceberg.rest-catalog.security=OAUTH2
      iceberg.rest-catalog.oauth2.credential=$${ENV:OPENLAKEFORGE_PEOPLE_${upper(stage)}_CLIENT_ID}:$${ENV:OPENLAKEFORGE_PEOPLE_${upper(stage)}_CLIENT_SECRET}
      iceberg.rest-catalog.oauth2.server-uri=${try(local.stage_catalog_contracts[stage].token_uri, "")}
      iceberg.rest-catalog.oauth2.scope=${try(local.stage_catalog_contracts[stage].oauth_scope, "")}
      iceberg.rest-catalog.session=USER
      # Zero rebuilds the session from the token each query carries; the
      # default hour would keep presenting the first one.
      iceberg.rest-catalog.session-timeout=0s
      iceberg.rest-catalog.vended-credentials-enabled=true
      iceberg.rest-catalog.nested-namespace-enabled=true
      ${local.people_s3_properties}
    CATALOG
  }

  # A person, whose user is an e-mail, may use the people catalogs and nothing
  # else; service principals never reach them.
  people_catalog_rules = [
    for name in compact([join("|", keys(try(var.people_access.catalogs, {})))]) : {
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
