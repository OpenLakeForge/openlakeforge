# People's Dex tokens (ADR 0019, build step 3). Polaris authenticates `mixed`:
# its own tokens first, so every workload keeps its credentials and does not
# depend on the issuer, then Dex's. Every person's token names one shared
# principal, the Dex connector id; the token's groups decide which of its
# principal roles are active.
locals {
  person_access_enabled = var.person_access != null

  # Each stage row of data_access is one of three shapes, which `olf`
  # validates: every layer write, every layer read, or Gold read alone.
  person_catalog_roles = {
    for role, stages in try(var.person_access.data_access, {}) : "olf-${role}" => {
      for stage, layers in stages : local.stage_catalogs[stage].catalog_name => (
        try(layers.gold, "") == "write" ? "olf-write" : length(layers) == 3 ? "olf-read" : "olf-gold-read"
      ) if contains(keys(local.stage_catalogs), stage)
    }
  }

  # A token whose groups map to no role would have an empty role set, which
  # Polaris answers by activating every role of the principal. The connector
  # id, which every token carries, is read as a role too and maps to
  # olf-signed-in, a role with no grant, so the set is never empty.
  person_role_mappings = concat(
    flatten([
      for role, claims in try(var.person_access.role_mapping, {}) : [
        for claim in claims : { regex = "^\\Q${claim}\\E$", replacement = "PRINCIPAL_ROLE:olf-${role}" }
      ] if contains(keys(try(var.person_access.data_access, {})), role)
    ]),
    [for name in compact([try(var.person_access.principal_name, "")]) : { regex = "^\\Q${name}\\E$", replacement = "PRINCIPAL_ROLE:olf-signed-in" }],
  )

  person_https = startswith(try(var.person_access.issuer_url, ""), "https://")

  person_values = !local.person_access_enabled ? [] : [yamlencode({
    authentication = { type = "mixed" }
    oidc = {
      authServeUrl    = var.person_access.issuer_url
      client          = { id = var.person_access.client_id }
      principalMapper = { nameClaimPath = "federated_claims/connector_id" }
      principalRolesMapper = {
        rolesClaimPath = "groups,federated_claims/connector_id"
        filter         = "^(${join("|", [for mapping in local.person_role_mappings : trimsuffix(trimprefix(mapping.regex, "^"), "$")])})$"
        mappings       = local.person_role_mappings
      }
    }
    advancedConfig = merge(
      {
        "quarkus.oidc.application-type" = "service"
        # Dex is served by Traefik, which may come up after Polaris; the
        # endpoints are set rather than discovered, and Polaris fetches the
        # keys on the first token it sees.
        "quarkus.oidc.discovery-enabled" = false
        "quarkus.oidc.jwks-path"         = "keys"
        "quarkus.oidc.token.issuer"      = var.person_access.issuer_url
        "quarkus.oidc.token.audience"    = var.person_access.client_id
      },
      local.person_https ? {
        "quarkus.oidc.tls.tls-configuration-name"        = "olf-local-ca"
        "quarkus.tls.olf-local-ca.trust-store.pem.certs" = "/etc/olf/oidc-ca/ca.crt"
      } : {},
    )
    extraVolumes      = local.person_https ? [{ name = "olf-oidc-ca", configMap = { name = var.person_access.ca_configmap_name } }] : []
    extraVolumeMounts = local.person_https ? [{ name = "olf-oidc-ca", mountPath = "/etc/olf/oidc-ca", readOnly = true }] : []
  })]

  person_access_script = !local.person_access_enabled ? "" : templatefile("${path.module}/templates/person-access.sh.tftpl", {
    person_access = jsonencode({
      principal = var.person_access.principal_name
      catalog_roles = {
        olf-write     = ["CATALOG_MANAGE_CONTENT"]
        olf-read      = ["TABLE_READ_DATA", "NAMESPACE_READ_PROPERTIES", "VIEW_READ_PROPERTIES"]
        olf-gold-read = ["NAMESPACE_LIST"]
      }
      catalogs = [for stage in values(local.stage_catalogs) : stage.catalog_name]
      # olf-signed-in is reconciled to hold no catalog role at all.
      assigned = merge({ olf-signed-in = {} }, local.person_catalog_roles)
    })
  })
}
