# The free-form lines of config.properties. The chart (1.42.2) takes them only
# from the top-level additionalConfigProperties, with no per-role list, and Helm
# replaces a list wholesale, so the module settles the whole list here and
# main.tf renders it once, after every other values file.
locals {
  # The password level's lines (auth_values in main.tf, ADR 0016).
  auth_config_properties = concat([
    "http-server.process-forwarded=true",
    "http-server.http.enabled=false",
    "discovery.uri=https://localhost:8443",
    "internal-communication.https.required=true",
    "internal-communication.https.truststore.path=/etc/trino/tls/ca.crt",
    "internal-communication.shared-secret=$${ENV:TRINO_INTERNAL_SECRET}",
    ],
    local.ui_properties,
    local.people_jwt_properties,
  )
  # The chart has no value for it either. Trino's default of 150 refused a
  # 75-table Gold query at 152 stages (#706).
  stage_count_properties = var.query_sizing == null ? [] : ["query.max-stage-count=${var.query_sizing.max_stage_count}"]
  # Without authentication the base values file's process-forwarded line stays,
  # for the requests Traefik forwards. The sign-in properties never come with
  # ui_properties: fixed is plain-HTTP only, OAuth2 HTTPS only.
  config_properties = concat(
    local.auth_enabled ? local.auth_config_properties : concat(["http-server.process-forwarded=true"], local.ui_properties),
    local.oidc_enabled ? local.oidc_properties : [],
    local.stage_count_properties,
  )
}
