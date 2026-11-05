locals {
  labels = {
    "app.kubernetes.io/name"       = "openmetadata"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "governance"
  }

  om_url              = "http://${var.release_name}.${var.namespace}:${var.om_http_port}"
  postgresql_ssl_mode = var.postgresql_ssl_mode != "" ? var.postgresql_ssl_mode : coalesce(try(var.postgresql_contract.ssl_mode, null), "disable")

  # OpenMetadata 1.13.6 reads authenticationConfiguration and
  # authorizerConfiguration from its environment only on the first boot of an
  # empty database; afterwards the stored copy wins (SettingsCache). Helm values
  # therefore cannot switch an existing install to SSO, and a fresh one would
  # lock olf's bootstrap out: custom-oidc answers POST /users/login with 403.
  # `olf infra openmetadata deploy-metadata` applies this body through
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
}
