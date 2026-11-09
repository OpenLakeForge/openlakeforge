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

  # A Glue catalog has no vended credentials and keeps reading with the storage
  # key until AWS moves to Polaris (#681). Named, not AWS_*: the keyless REST
  # catalogs read only with what Polaris vends, and the S3 client's default
  # chain would otherwise find this key in the environment (ADR 0019).
  glue_enabled = anytrue([for contract in values(local.stage_catalog_contracts) : try(contract.catalog_type, "rest") == "glue"])
  storage_secret_env = var.storage_contract.credentials_secret_name == null || !local.glue_enabled ? [] : [
    for name, key in {
      OPENLAKEFORGE_S3_ACCESS_KEY_ID     = coalesce(var.storage_contract.access_key_id_key, "AWS_ACCESS_KEY_ID")
      OPENLAKEFORGE_S3_SECRET_ACCESS_KEY = coalesce(var.storage_contract.secret_access_key_key, "AWS_SECRET_ACCESS_KEY")
    } : { name = name, valueFrom = { secretKeyRef = { name = var.storage_contract.credentials_secret_name, key = key } } }
  ]

  # The catalog's own credential, trino-catalog-<stage>, holds no grant.
  catalog_secret_env = flatten([
    for stage, contract in local.stage_catalog_contracts : try(contract.trino_credentials_secret_name, null) == null ? [] : [
      {
        name = "OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_ID"
        valueFrom = {
          secretKeyRef = {
            name = contract.trino_credentials_secret_name
            key  = coalesce(try(contract.trino_client_id_key, null), "POLARIS_CLIENT_ID")
          }
        }
      },
      {
        name = "OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_SECRET"
        valueFrom = {
          secretKeyRef = {
            name = contract.trino_credentials_secret_name
            key  = coalesce(try(contract.trino_client_secret_key, null), "POLARIS_CLIENT_SECRET")
          }
        }
      },
    ]
  ])

  s3_properties = join("\n", compact([
    "fs.native-s3.enabled=true",
    var.storage_contract.endpoint == null ? "" : "s3.endpoint=${var.storage_contract.endpoint}",
    var.storage_contract.path_style_access == null ? "" : "s3.path-style-access=${var.storage_contract.path_style_access}",
    "s3.region=${var.storage_contract.region}",
  ]))
  glue_s3_properties = join("\n", compact([
    local.s3_properties,
    length(local.storage_secret_env) == 0 ? "" : "s3.aws-access-key=$${ENV:OPENLAKEFORGE_S3_ACCESS_KEY_ID}",
    length(local.storage_secret_env) == 0 ? "" : "s3.aws-secret-key=$${ENV:OPENLAKEFORGE_S3_SECRET_ACCESS_KEY}",
  ]))

  # Keyless (ADR 0019 section 9): every table request carries the caller's own
  # Polaris token (the `token` extra credential) and reads files only with the
  # credentials Polaris vends for that table. The catalog's credential serves
  # only the configuration call, and a request without a token falls through
  # to a token exchange Polaris refuses (#964). Keyed by stage: the people
  # catalog (people.tf) is the same catalog under the stage's name.
  rest_catalog_properties = {
    for stage, contract in local.stage_catalog_contracts : stage => <<-CATALOG
      # openlakeforge.catalog-provider=${coalesce(try(contract.catalog_provider, null), "polaris")}
      connector.name=iceberg
      iceberg.catalog.type=rest
      iceberg.rest-catalog.uri=${try(contract.rest_uri, "")}
      iceberg.rest-catalog.warehouse=${try(contract.warehouse, "")}
      iceberg.rest-catalog.security=OAUTH2
      iceberg.rest-catalog.oauth2.credential=$${ENV:OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_ID}:$${ENV:OPENLAKEFORGE_CATALOG_${upper(stage)}_CLIENT_SECRET}
      iceberg.rest-catalog.oauth2.server-uri=${try(contract.token_uri, "")}
      iceberg.rest-catalog.oauth2.scope=${try(contract.oauth_scope, "")}
      iceberg.rest-catalog.session=USER
      # Zero rebuilds the session from the token each query carries; the
      # default hour would keep presenting the first one.
      iceberg.rest-catalog.session-timeout=0s
      # Iceberg builds the caller's session from a copy of the catalog's
      # config, credential included, and reads the token's `exp`. A token
      # already expired when the session is built is "refreshed" with that
      # credential (OAuth2Util.AuthSession.refresh, refreshExpiredToken),
      # which swaps the caller for trino-catalog-<stage>. Off, an expired
      # token reaches Polaris as itself and is refused (#1012).
      iceberg.rest-catalog.oauth2.token-refresh-enabled=false
      iceberg.rest-catalog.vended-credentials-enabled=true
      iceberg.rest-catalog.nested-namespace-enabled=true
      ${local.s3_properties}
    CATALOG
    if try(contract.catalog_type, "rest") != "glue"
  }

  iceberg_catalog_properties = {
    for stage, contract in local.stage_catalog_contracts : contract.catalog_name => (
      try(contract.catalog_type, "rest") == "glue" ? <<-CATALOG
        # openlakeforge.catalog-provider=aws-glue
        connector.name=iceberg
        iceberg.catalog.type=glue
        hive.metastore.glue.region=${coalesce(try(contract.glue_region, null), var.storage_contract.region)}
        hive.metastore.glue.catalogid=${try(contract.glue_catalog_id, "")}
        ${local.glue_s3_properties}
      CATALOG
      : local.rest_catalog_properties[stage]
    )
  }
  # Polaris decides every access on a keyless catalog (ADR 0019 section 9),
  # so Trino carries no rules there. A Glue catalog is not keyless: until AWS
  # moves to Polaris (#681) its stages share one coordinator, and these rules
  # by claimed user name are the only thing keeping one stage's catalog from
  # another's (#983 keeps them for Glue only).
  catalog_access_rules = jsonencode({
    catalogs = concat(
      [
        for stage, contract in local.stage_catalog_contracts : {
          user    = "olf-${stage}-runtime"
          catalog = contract.catalog_name
          allow   = "all"
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
  })

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

  # A Polaris catalog's credential Secret is written by olf's bootstrap after
  # the apply (#562), so on a fresh install, or with a new stage, the pods wait
  # in CreateContainerConfigError until it exists and Helm cannot wait for
  # them. olf restarts Trino when it writes one and then waits for the rollout.
  wait    = length(local.catalog_secret_env) == 0
  timeout = 600

  values = concat([
    file(var.base_values_file),
    ], local.sizing_values, [
    yamlencode(merge({
      env = concat(local.storage_secret_env, local.catalog_secret_env)

      # The stage's REST catalog is served under its own name too: Superset,
      # `olf query` and the platform API name the catalog after the stage.
      catalogs = merge(local.iceberg_catalog_properties, local.rest_catalog_properties)

      serviceAccount = {
        create      = local.create_service_account
        name        = local.create_service_account ? local.service_account_name : ""
        annotations = var.service_account_annotations
      }
      }, {
      # A for expression, not a conditional: without a Glue catalog there is no
      # access control at all, and the two branches would not share a type.
      for key, value in {
        accessControl = {
          type       = "configmap"
          configFile = "rules.json"
          rules      = { "rules.json" = local.catalog_access_rules }
        }
      } : key => value if local.glue_enabled
    })),
    ],
    # Last, because Helm replaces a list wholesale (config.tf).
    [yamlencode({ additionalConfigProperties = local.config_properties })],
  )

  lifecycle {
    # A keyless catalog reads through the AWS SDK default chain outside vended
    # credentials (#963), so a role on the pods would read storage with it.
    precondition {
      condition     = length(local.rest_catalog_properties) == 0 || (length(var.service_account_annotations) == 0 && var.service_account_name == "")
      error_message = "A keyless (Polaris) Trino catalog runs with no cloud workload identity: unset service_account_annotations and service_account_name."
    }
  }
}
