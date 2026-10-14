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

  storage_secret_env_from = var.storage_contract.credentials_secret_name == null ? [] : [
    {
      secretRef = {
        name = var.storage_contract.credentials_secret_name
      }
    },
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
    var.storage_contract.credentials_secret_name == null ? "" : "s3.aws-access-key=$${ENV:AWS_ACCESS_KEY_ID}",
    var.storage_contract.credentials_secret_name == null ? "" : "s3.aws-secret-key=$${ENV:AWS_SECRET_ACCESS_KEY}",
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
  # olf/contracts.py) - Trino has no authentication configured, so this
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
    # namespace_prefix (olf.contracts.resolve_physical_names) as a second,
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
  }, local.principal_rules))

  auth_enabled = var.workload_authentication_enabled
  # Each verified principal may become exactly its own user, so a DEV password
  # cannot claim `olf-prod-runtime` through X-Trino-User; the last rule denies
  # every other principal (ADR 0016). A for expression, not a conditional, so
  # the disabled case is an empty object of the same type.
  principal_rules = {
    for key, rules in {
      principals = concat(
        [for principal in local.credential_principals : { principal = principal, user = principal, allow = true }],
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
  # Password authentication on an HTTPS listener (ADR 0016). The HTTP port
  # stays for the chart's probes: Trino refuses a query or a password on it.
  # `http-server.process-forwarded` is deliberately not set: with it on, a pod
  # can send `X-Forwarded-Proto: https` to the HTTP port and have a password
  # accepted there (docs/testing/trino_workload_auth_proof.py, case k4).
  auth_values = {
    server = {
      config = {
        authenticationType = "PASSWORD"
        https = {
          enabled  = true
          port     = 8443
          keystore = { path = "/etc/trino/tls/tls-combined.pem" }
        }
      }
    }
    auth                       = { passwordAuthSecret = "trino-password-file" }
    additionalConfigProperties = ["internal-communication.shared-secret=$${ENV:TRINO_INTERNAL_SECRET}"]
    coordinator = {
      # Mounted as a directory so a renewed Secret reaches the pod; Trino does
      # not reload it, so the coordinator restarts to pick it up.
      secretMounts = [{ name = "tls", secretName = local.tls_secret_name, path = "/etc/trino/tls" }]
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
    yamlencode({
      envFrom = local.storage_secret_env_from
      env     = concat(local.catalog_secret_env, local.internal_secret_env)

      catalogs = local.iceberg_catalog_properties

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
  ], local.auth_enabled ? [yamlencode(local.auth_values)] : [])

  lifecycle {
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
  count = local.auth_enabled ? 1 : 0

  name      = "${var.release_name}-certificate"
  chart     = "${path.module}/certificate"
  namespace = var.namespace

  values = [yamlencode({
    secretName = local.tls_secret_name
    issuerName = var.tls_issuer_name
    dnsNames   = local.tls_dns_names
  })]
}
