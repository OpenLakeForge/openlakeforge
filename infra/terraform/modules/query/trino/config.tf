# The free-form lines of config.properties. The chart (1.42.2) takes them only
# from the top-level additionalConfigProperties, with no per-role list, and Helm
# replaces a list wholesale, so the module settles the whole list here and
# main.tf renders it once, after every other values file.
locals {
  # The chart has no value for it either. Trino's default of 150 refused a
  # 75-table Gold query at 152 stages (#706).
  stage_count_properties = var.query_sizing == null ? [] : ["query.max-stage-count=${var.query_sizing.max_stage_count}"]
  # process-forwarded keeps the base values file's line, for the requests
  # Traefik forwards.
  config_properties = concat(["http-server.process-forwarded=true"], local.ui_properties, local.stage_count_properties)
}
