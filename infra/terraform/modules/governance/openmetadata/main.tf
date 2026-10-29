locals {
  labels = {
    "app.kubernetes.io/name"       = "openmetadata"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "governance"
  }

  om_url                        = "http://${var.release_name}.${var.namespace}:${var.om_http_port}"
  catalog_schema_names_json     = jsonencode(var.catalog_schema_names)
  catalog_schema_names_json_b64 = base64encode(local.catalog_schema_names_json)
  catalog_type                  = coalesce(try(var.catalog_contract.catalog_type, null), "rest")
  catalog_service_name          = local.catalog_type == "glue" ? "aws_glue" : "polaris"
  catalog_service_display_name  = "Lakehouse catalogs (Trino)"
  # OpenMetadata 1.13 removed its Iceberg connector, so the lakehouse service
  # crawls through Trino, where every stage is one catalog (lakehouse_<stage>)
  # and so one database. The service keeps its name, so FQNs and the lineage
  # namespace mapping are unchanged.
  trino_host_port       = trimprefix(var.trino_lineage_namespace, "trino://")
  catalog_database_name = var.stages[var.canonical_stage].catalog_database_name
  catalog_database_fqn  = "${local.catalog_service_name}.${local.catalog_database_name}"
  postgresql_ssl_mode   = var.postgresql_ssl_mode != "" ? var.postgresql_ssl_mode : coalesce(try(var.postgresql_contract.ssl_mode, null), "disable")

  # OpenMetadata 1.13.6 reads authenticationConfiguration and
  # authorizerConfiguration from its environment only on the first boot of an
  # empty database; afterwards the stored copy wins (SettingsCache). Helm values
  # therefore cannot switch an existing install to SSO, and a fresh one would
  # lock the bootstrap job out: custom-oidc answers POST /users/login with 403.
  # `olf openmetadata deploy-metadata` applies this body through
  # PUT /api/v1/system/security/config, adding the client secret, once the
  # issuer's host resolves in the cluster (after Traefik, #432).
  oidc_issuer     = var.oidc == null ? "" : trimsuffix(var.oidc.issuer_url, "/")
  base_authorizer = yamldecode(file(var.base_values_file)).openmetadata.config.authorizer
  oidc_security_config = var.oidc == null ? null : {
    authenticationConfiguration = {
      provider     = "custom-oidc"
      providerName = "OpenLakeForge"
      clientType   = "confidential"
      authority    = var.oidc.issuer_url
      clientId     = var.oidc.client_id
      callbackUrl  = "${var.oidc.base_url}/callback"
      responseType = "id_token"
      # OpenMetadata's own keys stay trusted beside the issuer's: bot tokens
      # (ingestion-bot, the automation bot) are signed by OpenMetadata. Dex,
      # the only issuer deployed here, serves its keys at /keys.
      publicKeyUrls            = ["${local.om_url}/api/v1/system/config/jwks", "${local.oidc_issuer}/keys"]
      tokenValidationAlgorithm = "RS256"
      # The login of the person (ADR 0014): the role sync joins on it, email never.
      jwtPrincipalClaims        = ["preferred_username"]
      jwtPrincipalClaimsMapping = []
      enableSelfSignup          = true
      enableAutoRedirect        = true
      forceSecureSessionCookie  = startswith(var.oidc.base_url, "https")
      oidcConfiguration = {
        type                       = "custom"
        id                         = var.oidc.client_id
        scope                      = var.oidc.scope
        discoveryUri               = "${local.oidc_issuer}/.well-known/openid-configuration"
        useNonce                   = "true"
        preferredJwsAlgorithm      = "RS256"
        responseType               = "code"
        disablePkce                = true
        callbackUrl                = "${var.oidc.base_url}/callback"
        serverUrl                  = var.oidc.base_url
        clientAuthenticationMethod = "client_secret_post"
        tenant                     = ""
        # Session policy (#177). tokenValidity applies only when a refresh
        # response has no id_token; max_age and prompt would make the issuer
        # re-authenticate on every renewal.
        tokenValidity = "900"
        maxAge        = ""
        prompt        = ""
      }
    }
    authorizerConfiguration = {
      className              = local.base_authorizer.className
      containerRequestFilter = local.base_authorizer.containerRequestFilter
      # Empty: the local admin has no login under SSO, and a listed name is made an admin at every start (OM-11).
      adminPrincipals                 = []
      allowedEmailRegistrationDomains = local.base_authorizer.allowedEmailRegistrationDomains
      principalDomain                 = local.base_authorizer.principalDomain
      allowedDomains                  = local.base_authorizer.allowedDomains
      enforcePrincipalDomain          = local.base_authorizer.enforcePrincipalDomain
      enableSecureSocketConnection    = false
      useRolesFromProvider            = false
    }
  }

  # See the Polaris module: an immutable Job template needs a new name for
  # the ingestion-bot Secret replicas to be created for a newly added stage.
  bootstrap_script = templatefile("${path.module}/templates/bootstrap.sh.tftpl", {
    om_url                        = local.om_url
    admin_email                   = var.admin_email
    admin_secret_name             = var.admin_secret_name
    catalog_service_name          = local.catalog_service_name
    catalog_service_display_name  = local.catalog_service_display_name
    ingestion_bot_secret_name     = var.ingestion_bot_secret_name
    trino_lineage_namespace       = var.trino_lineage_namespace
    trino_host_port               = local.trino_host_port
    ingestion_bot_jwt_key         = var.ingestion_bot_jwt_key
    catalog_schema_names_json_b64 = local.catalog_schema_names_json_b64
    stages_json_b64               = base64encode(jsonencode(var.stages))
    lineage_pipeline_service      = var.stages[var.canonical_stage].pipeline_service_name
    superset_username             = var.superset_username
    superset_admin_secret_name    = var.superset_admin_secret_name
    superset_auth_provider        = var.superset_auth_provider
    superset_verify_ssl           = var.superset_verify_ssl
    sso_enabled                   = local.oidc_security_config == null ? "" : "true"
    automation_secret_name        = var.automation_secret_name
    automation_jwt_key            = var.automation_jwt_key
  })

  bootstrap_job_name = "openmetadata-bootstrap-${helm_release.openmetadata.metadata.revision}"

  # Keyed on the whole bootstrap job -- its name and its rendered script --
  # as well as the namespace set. That job mints the credentials being
  # copied, and a Job spec is immutable, so any change to the script
  # replaces it and re-mints them. Keying on the name alone would miss every
  # replacement that keeps the Helm revision, leaving each namespace with a
  # token the service no longer accepts.
  workload_revision = substr(
    sha256(join(",", concat(
      sort(var.workload_namespaces),
      ["revoked"],
      sort(var.revoked_namespaces),
      [local.bootstrap_job_name, sha256(local.bootstrap_script)],
    ))),
    0,
    8,
  )

  bootstrap_annotations = {
    "openlakeforge.io/openmetadata-release-revision" = tostring(helm_release.openmetadata.metadata.revision)
    "openlakeforge.io/catalog-schema-hash"           = sha256(local.catalog_schema_names_json)
  }
}
