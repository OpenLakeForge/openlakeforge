# The free-form lines of config.properties. The chart (1.42.2) takes them only
# from the top-level additionalConfigProperties, with no per-role list, and Helm
# replaces a list wholesale, so the module settles the whole list here and
# main.tf renders it once, after every other values file.
locals {
  # The chart has no value for it either. Trino's default of 150 refused a
  # 75-table Gold query at 152 stages (#706).
  stage_count_properties = var.query_sizing == null ? [] : ["query.max-stage-count=${var.query_sizing.max_stage_count}"]
  # The JVM heap beside /metrics' default series, for ADR 0024's Trino memory
  # chart; Trino adds these beans to its defaults, never replaces them. Set
  # whether or not metrics are scraped: the endpoint is served either way, and
  # following the profile would restart Trino whenever metrics are toggled.
  metrics_properties = ["openmetrics.jmx-object-names=java.lang:type=Memory"]
  # Trino 480's HTTP server (airlift 419, Jetty 12.1.7) compresses GET and POST
  # responses by default, and its compression handler intermittently ends a
  # gzip response after the 10-byte gzip header: the connection closes
  # mid-body, so Traefik aborts the client's and `olf query` fails with
  # ChunkedEncodingError (#1088). Uncompressed, a large result page costs
  # bandwidth instead of failing.
  compression_properties = ["http-server.compression.enabled=false"]
  # process-forwarded keeps the base values file's line, for the requests
  # Traefik forwards.
  config_properties = concat(["http-server.process-forwarded=true"], local.compression_properties, local.metrics_properties, local.ui_properties, local.stage_count_properties)
}
