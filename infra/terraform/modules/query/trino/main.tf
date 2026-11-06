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
  # Password authentication over the plain HTTP listener (ADR 0016): the pod
  # network is the trust boundary, so Trino is told to accept a password there.
  auth_values = {
    server = {
      config = {
        authenticationType = local.people_enabled ? "PASSWORD,JWT" : "PASSWORD"
      }
    }
    auth                       = { passwordAuthSecret = "trino-password-file" }
    additionalConfigProperties = local.auth_config_properties
  }
  # Only a deployment whose ingress is HTTPS needs it: the Web UI sign-in and
  # the signing-key fetch call the issuer's public URL, signed by the local CA.
  ca_mount_path = "/etc/trino/ca"
  ca_values = var.ca_configmap_name == null ? {} : {
    coordinator = {
      configMounts = [{ name = "ca", configMap = var.ca_configmap_name, path = local.ca_mount_path }]
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

  # A Polaris catalog's credential Secret is written by olf's bootstrap after
  # the apply (#562), so on a fresh install, or with a new stage, the pods wait
  # in CreateContainerConfigError until it exists and Helm cannot wait for
  # them. olf restarts Trino when it writes one and then waits for the rollout.
  wait    = length(local.catalog_secret_env) == 0
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
    ], local.auth_enabled ? [yamlencode(local.auth_values)] : [], local.auth_enabled ? [yamlencode(local.ca_values)] : [],
    # Last, because Helm replaces a list wholesale (config.tf).
    [yamlencode({ additionalConfigProperties = local.config_properties })],
  )

  lifecycle {
    # Workers with authentication on were never run: the shared secret and the
    # password file are wired for the coordinator only. Multi-node with
    # authentication is #367.
    precondition {
      # A conditional, not ||: Terraform evaluates both operands, and .workers on null fails the plan.
      condition     = var.query_sizing == null ? true : (var.query_sizing.workers == 0 || !local.auth_enabled)
      error_message = "spec.query.size medium and large add Trino workers, which are not supported with Trino workload authentication (password): deploy without spec.access, or use size small."
    }
    # The principals rule is built from stage_namespaces and the catalog rules
    # from the catalog contracts; a stage in only one would be locked out.
    precondition {
      condition     = !local.auth_enabled || toset(keys(var.stage_namespaces)) == toset(keys(local.stage_catalog_contracts))
      error_message = "With workload authentication on, stage_namespaces must name exactly the stages in the catalog contracts."
    }
  }

  # The password file and internal secret must exist before the coordinator
  # starts with authentication on. Nothing here when it is off.
  depends_on = [
    kubernetes_job_v1.credentials,
  ]
}
