locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "trino"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version
  # `tomap(...)`, not a bare object literal: `{ dev = var.catalog_contract }`
  # infers as object({dev=...}), which does not unify with
  # var.stage_catalog_contracts's map(any) type in the conditional below and
  # fails at apply time with "Inconsistent conditional result types" even
  # though `terraform validate` accepts it (validate does not evaluate the
  # branch not taken with a concrete value).
  stage_catalog_contracts = length(var.stage_catalog_contracts) > 0 ? var.stage_catalog_contracts : tomap({
    dev = var.catalog_contract
  })

  # Named, not AWS_*: the people catalogs carry no key and must read only with
  # the credentials Polaris vends, and the S3 client's default chain would
  # otherwise find this key in the environment (ADR 0019).
  storage_secret_env = var.storage_contract.credentials_secret_name == null ? [] : [
    for name, key in {
      OPENLAKEFORGE_S3_ACCESS_KEY_ID     = coalesce(var.storage_contract.access_key_id_key, "AWS_ACCESS_KEY_ID")
      OPENLAKEFORGE_S3_SECRET_ACCESS_KEY = coalesce(var.storage_contract.secret_access_key_key, "AWS_SECRET_ACCESS_KEY")
    } : { name = name, valueFrom = { secretKeyRef = { name = var.storage_contract.credentials_secret_name, key = key } } }
  ]

  catalog_secret_env = flatten([
    for stage, contract in local.stage_catalog_contracts : try(contract.trino_credentials_secret_name, null) == null ? [] : [
      {
        name = "OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_ID"
        valueFrom = {
          secretKeyRef = {
            name = contract.trino_credentials_secret_name
            key  = coalesce(try(contract.trino_client_id_key, null), "POLARIS_TRINO_CLIENT_ID")
          }
        }
      },
      {
        name = "OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_SECRET"
        valueFrom = {
          secretKeyRef = {
            name = contract.trino_credentials_secret_name
            key  = coalesce(try(contract.trino_client_secret_key, null), "POLARIS_TRINO_CLIENT_SECRET")
          }
        }
      },
    ]
  ])

  s3_catalog_properties = join("\n", compact([
    "fs.native-s3.enabled=true",
    var.storage_contract.endpoint == null ? "" : "s3.endpoint=${var.storage_contract.endpoint}",
    var.storage_contract.path_style_access == null ? "" : "s3.path-style-access=${var.storage_contract.path_style_access}",
    "s3.region=${var.storage_contract.region}",
    var.storage_contract.credentials_secret_name == null ? "" : "s3.aws-access-key=$${ENV:OPENLAKEFORGE_S3_ACCESS_KEY_ID}",
    var.storage_contract.credentials_secret_name == null ? "" : "s3.aws-secret-key=$${ENV:OPENLAKEFORGE_S3_SECRET_ACCESS_KEY}",
  ]))

  iceberg_catalog_properties = {
    for stage, contract in local.stage_catalog_contracts : contract.catalog_name => (
      try(contract.catalog_type, "rest") == "glue" ? <<-CATALOG
        # openlakeforge.catalog-provider=aws-glue
        connector.name=iceberg
        iceberg.catalog.type=glue
        hive.metastore.glue.region=${coalesce(try(contract.glue_region, null), var.storage_contract.region)}
        hive.metastore.glue.catalogid=${try(contract.glue_catalog_id, "")}
        ${local.s3_catalog_properties}
      CATALOG
      : <<-CATALOG
        # openlakeforge.catalog-provider=${coalesce(try(contract.catalog_provider, null), "polaris")}
        # openlakeforge.polaris-bootstrap-run=${try(contract.bootstrap_run_id, "")}
        # openlakeforge.polaris-bootstrap-revision=${var.catalog_bootstrap_revision}
        connector.name=iceberg
        iceberg.catalog.type=rest
        iceberg.rest-catalog.uri=${try(contract.rest_uri, "")}
        iceberg.rest-catalog.warehouse=${try(contract.warehouse, "")}
        iceberg.rest-catalog.security=OAUTH2
        iceberg.rest-catalog.oauth2.credential=$${ENV:OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_ID}:$${ENV:OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_SECRET}
        iceberg.rest-catalog.oauth2.server-uri=${try(contract.token_uri, "")}
        iceberg.rest-catalog.oauth2.scope=${try(contract.oauth_scope, "")}
        iceberg.rest-catalog.vended-credentials-enabled=false
        iceberg.rest-catalog.nested-namespace-enabled=true
        ${local.s3_catalog_properties}
      CATALOG
    )
  }
  # Every stage-scoped client (dbt, Dagster, Superset, and the e2e suite)
  # authenticates to Trino as its own stage's runtime identity
  # (`olf-<stage>-runtime`, see `runtime_identity_principal` in
  # olf/infra/contracts.py) - Trino has no authentication configured, so this
  # string match is the only thing that keeps a DEV client from resolving
  # PROD's catalog. `system` stays read-only for cluster metadata; every
  # other catalog is denied unless explicitly listed above.
  catalog_access_rules = jsonencode(merge({
    catalogs = concat(
      [
        for stage, contract in local.stage_catalog_contracts : {
          user    = "olf-${stage}-runtime"
          catalog = contract.catalog_name
          allow   = "all"
        }
      ],
      # OpenMetadata crawls every stage's catalog into that stage's own
      # root through its Trino service (modules/governance/openmetadata).
      [
        for contract in values(local.stage_catalog_contracts) : {
          user    = "openmetadata"
          catalog = contract.catalog_name
          allow   = "read-only"
        }
      ],
      local.people_catalog_rules,
      [
        { catalog = "system", allow = "read-only" },
        { catalog = ".*", allow = "none" },
      ],
    )
    # A Glue-provider catalog is only a per-stage catalog by name: every
    # stage's Trino catalog resolves to the same shared account Glue
    # catalog ID (this account's Glue service refuses to create a real
    # per-stage one), so a stage's schemas are physically visible from
    # every other Glue-provider stage's catalog too. The `catalogs` rule
    # above only gates which catalog a client can use at all - it does not
    # stop olf-dev-runtime from reading a schema that belongs to prod once
    # inside its own catalog. Restrict table privileges to this stage's own
    # namespace_prefix (olf.infra.contracts.resolve_physical_names) as a second,
    # schema-level layer on top of the IAM policy that already scopes each
    # stage's Pod Identity role to its own `database/<prefix>_*` ARNs -
    # Polaris-backed stages need no such rule: each already has its own
    # separate Iceberg catalog, a real boundary Trino's catalog rule alone
    # is enough to enforce.
    tables = concat(
      flatten([
        for stage, contract in local.stage_catalog_contracts : contract.catalog_provider == "aws-glue" ? [
          {
            user       = "olf-${stage}-runtime"
            catalog    = contract.catalog_name
            schema     = "${contract.catalog_name}_.*"
            privileges = ["SELECT", "INSERT", "DELETE", "UPDATE", "OWNERSHIP"]
          },
          {
            user       = "olf-${stage}-runtime"
            catalog    = contract.catalog_name
            schema     = ".*"
            privileges = []
          },
        ] : []
      ]),
      [{ privileges = ["SELECT", "INSERT", "DELETE", "UPDATE", "OWNERSHIP"] }],
    )
  }, local.principal_rules, local.people_query_rules))

  # Overlay on the base values file. For `small` it restates what the file
  # already sets (plus the chart's own query.max-memory), so the rendered
  # manifests do not change; for larger sizes it adds the worker deployment.
  sizing_node_values = {
    for role, node in {
      coordinator = try(var.query_sizing.coordinator, null)
      worker      = try(var.query_sizing.worker, null)
      } : role => {
      jvm = { maxHeapSize = node.heap }
      config = merge(
        { query = { maxMemoryPerNode = var.query_sizing.max_memory_per_node } },
        { for key, value in { nodeScheduler = { includeCoordinator = var.query_sizing.schedule_on_coordinator } } : key => value if role == "coordinator" },
      )
      # The chart has no value for this property, and the top-level list is
      # replaced wholesale by the TLS and auth values, so it rides in the
      # coordinator's own list, which nothing else sets. Only the coordinator plans stages.
      additionalConfigProperties = role == "coordinator" ? ["query.max-stage-count=${var.query_sizing.max_stage_count}"] : []
      resources = {
        requests = { cpu = node.cpu_request, memory = node.memory_request }
        limits   = { memory = node.memory_limit }
      }
    } if node != null
  }
  sizing_values = var.query_sizing == null ? [] : [yamlencode(merge(local.sizing_node_values, {
    server = {
      workers = var.query_sizing.workers
      config  = { query = { maxMemory = var.query_sizing.max_memory } }
    }
  }))]

  tls_enabled  = var.workload_authentication != "off"
  auth_enabled = var.workload_authentication == "password"
  # Each verified principal may become exactly its own user, so a DEV password
  # cannot claim `olf-prod-runtime` through X-Trino-User; the last rule denies
  # every other principal (ADR 0016). A for expression, not a conditional, so
  # the disabled case is an empty object of the same type.
  principal_rules = {
    for key, rules in {
      principals = concat(
        [for principal in local.credential_principals : { principal = principal, user = principal, allow = true }],
        local.people_principal_rules,
        [{ principal = ".*", user = ".*", allow = false }],
      )
    } : key => rules if local.auth_enabled
  }
  tls_secret_name = "trino-tls"
  tls_dns_names = [
    var.release_name,
    "${var.release_name}.${var.namespace}",
    "${var.release_name}.${var.namespace}.svc",
    "${var.release_name}.${var.namespace}.svc.cluster.local",
    "localhost", # the Trino CLI inside the coordinator pod (olf e2e)
  ]
  # The shared secret that authenticated nodes need for internal requests comes
  # from the environment because config.properties accepts ${ENV:...}.
  internal_secret_env = local.auth_enabled ? [
    {
      name = "TRINO_INTERNAL_SECRET"
      valueFrom = {
        secretKeyRef = {
          name = "trino-internal-secret"
          key  = "internal-communication.shared-secret"
        }
      }
    },
  ] : []
  tls_ca_secret_name = "trino-ca-bundle"
  traefik_transport  = "trino"
  # The tls level: an HTTPS listener beside the HTTP one, no authentication, so
  # callers can move to HTTPS and prove the CA while still sending only a user
  # name (ADR 0016 migration step 2-3).
  tls_values = {
    server = {
      config = {
        https = {
          enabled  = true
          port     = 8443
          keystore = { path = "/etc/trino/tls/tls-combined.pem" }
        }
      }
    }
    coordinator = {
      # Mounted as a directory so a renewed Secret reaches the pod; Trino does
      # not reload it, so the coordinator restarts to pick it up.
      secretMounts = [{ name = "tls", secretName = local.tls_secret_name, path = "/etc/trino/tls" }]
    }
  }
  # Password authentication on an HTTPS-only coordinator (ADR 0016). There is
  # no HTTP listener, so nothing can downgrade a password to cleartext, and
  # `process-forwarded` stays on for the requests Traefik re-encrypts to 8443.
  # A node reaches itself at https://localhost:8443, a name in the certificate,
  # with the CA as its truststore; the chart's own discovery.uri comes first and
  # the later line wins (proof k1).
  auth_values = {
    server = {
      config = {
        authenticationType = local.people_enabled ? "PASSWORD,JWT" : "PASSWORD"
      }
    }
    auth = { passwordAuthSecret = "trino-password-file" }
    additionalConfigProperties = concat([
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
    additionalNodeProperties = ["node.internal-address=localhost"]
    # Traefik reaches Trino over HTTPS, trusting the CA through the file-provider
    # transport of the same name (modules/access/traefik).
    service = {
      annotations = {
        "traefik.ingress.kubernetes.io/service.serversscheme"    = "https"
        "traefik.ingress.kubernetes.io/service.serverstransport" = "${local.traefik_transport}@file"
      }
    }
    coordinator = {
      # The chart hardwires the liveness probe to the HTTP port, which no longer
      # exists, and takes only its timings. The readiness and startup probes run
      # the image's health-check, which uses HTTPS when HTTP is disabled, so
      # these values only make the liveness failure effectively inert: it fires
      # once an hour and would restart the pod after 1000 failures.
      livenessProbe = { periodSeconds = 3600, failureThreshold = 1000 }
    }
  }
  # Create a named service account when one is explicitly requested (EKS Pod Identity
  # binds credentials by SA name and needs no annotation) or, IRSA-style, when
  # annotations are supplied. Otherwise Trino runs under the namespace default SA.
  service_account_name   = var.service_account_name != "" ? var.service_account_name : "trino"
  create_service_account = var.service_account_name != "" || length(var.service_account_annotations) > 0
}

resource "helm_release" "trino" {
  name       = var.release_name
  repository = local.repository
  chart      = local.chart
  version    = local.version
  namespace  = var.namespace

  wait    = true
  timeout = 600

  values = concat([
    file(var.base_values_file),
    ], local.sizing_values, [
    yamlencode({
      env = concat(local.storage_secret_env, local.catalog_secret_env, local.internal_secret_env, local.oidc_env, local.people_catalog_env)

      catalogs = merge(local.iceberg_catalog_properties, local.people_catalog_properties)

      accessControl = {
        type       = "configmap"
        configFile = "rules.json"
        rules = {
          "rules.json" = local.catalog_access_rules
        }
      }

      serviceAccount = {
        create      = local.create_service_account
        name        = local.create_service_account ? local.service_account_name : ""
        annotations = var.service_account_annotations
      }
    }),
    ], local.tls_enabled ? [yamlencode(local.tls_values)] : [], local.auth_enabled ? [yamlencode(local.auth_values)] : [],
    # With auth on, ui_properties ride in auth_values (a later values file replaces a list, so the base process-forwarded line is repeated here).
    !local.auth_enabled && length(local.ui_properties) > 0 ? [yamlencode({ additionalConfigProperties = concat(["http-server.process-forwarded=true"], local.ui_properties) })] : [],
    # Last, because Helm replaces a list wholesale: this one carries the list
    # the levels above settled on, plus the sign-in properties. Never set
    # together with ui_properties: fixed is plain-HTTP only, OAuth2 HTTPS only.
    local.oidc_enabled ? [yamlencode({
      additionalConfigProperties = concat(
        local.auth_enabled ? local.auth_values.additionalConfigProperties : ["http-server.process-forwarded=true"],
        local.oidc_properties,
      )
    })] : [],
  )

  lifecycle {
    # The TLS and password values (below) point every node at the coordinator
    # as localhost and mount the certificate on the coordinator only, so a
    # worker would register with itself. Multi-node with authentication is #367.
    precondition {
      # A conditional, not ||: Terraform evaluates both operands, and .workers on null fails the plan.
      condition     = var.query_sizing == null ? true : (var.query_sizing.workers == 0 || !local.tls_enabled)
      error_message = "spec.query.size medium and large add Trino workers, which are not supported with Trino workload authentication (tls or password): deploy without spec.access, or use size small."
    }
    # The principals rule is built from stage_namespaces and the catalog rules
    # from the catalog contracts; a stage in only one would be locked out.
    precondition {
      condition     = !local.auth_enabled || toset(keys(var.stage_namespaces)) == toset(keys(local.stage_catalog_contracts))
      error_message = "With workload authentication on, stage_namespaces must name exactly the stages in the catalog contracts."
    }
  }

  # The password file, internal secret and certificate must exist before the
  # coordinator starts with authentication on. Nothing here when it is off.
  depends_on = [
    kubernetes_job_v1.credentials,
    helm_release.certificate,
  ]
}

# A release of its own: Certificate is a cert-manager CRD kind, which Helm
# cannot map until cert-manager has installed it (same reason as local-ca).
resource "helm_release" "certificate" {
  count = local.tls_enabled ? 1 : 0

  name      = "${var.release_name}-certificate"
  chart     = "${path.module}/certificate"
  namespace = var.namespace

  values = [yamlencode({
    secretName      = local.tls_secret_name
    trustSecretName = local.tls_ca_secret_name
    issuerName      = var.tls_issuer_name
    dnsNames        = local.tls_dns_names
  })]
}
