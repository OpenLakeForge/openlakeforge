# Superset exchanges codes, fetches keys and requests its per-user Trino token
# at the issuer's Service (ADR 0013, decision 8, #991), so the pod mounts no
# CA ConfigMap for an issuer call and imports no CA into its trust store.

mock_provider "helm" {}
mock_provider "kubernetes" {}

variables {
  namespace           = "olf-dev"
  base_values_file    = "tests/values.yaml"
  bootstrap_job_image = "alpine/k8s:test"
  postgresql_contract = {
    host                             = "postgresql.olf-system"
    port                             = 5432
    superset_db_name                 = "superset"
    superset_db_user                 = "superset"
    superset_credentials_secret_name = "superset-db"
  }
  oidc = {
    issuer_url            = "https://auth.olf.localhost"
    internal_url          = "http://dex.olf-system.svc.cluster.local:5556"
    role_claim            = "groups"
    role_mapping          = { viewer = ["acme:viewers"] }
    grants                = { viewer = "read-only" }
    sign_out_url          = "https://oauth2.olf.localhost/oauth2/sign_out"
    scope                 = "openid email groups"
    client_id             = "superset"
    secret_ref            = { name = "dex-client-superset", key = "client-secret" }
    secret_namespace      = "olf-system"
    database_redirect_uri = "https://superset-dev.olf.localhost/api/v1/database/oauth2/"
    people_catalog        = "dev"
  }
}

run "no_issuer_call_mounts_a_ca" {
  command = plan

  assert {
    condition = alltrue([
      for doc in helm_release.superset.values :
      !strcontains(doc, "olf-local-ca") && !strcontains(doc, "oidc-ca") && !strcontains(doc, "REQUESTS_CA_BUNDLE")
    ])
    error_message = "Superset must not mount or import the local CA: it reaches the issuer over its Service"
  }

  assert {
    condition     = anytrue([for doc in helm_release.superset.values : strcontains(doc, "http://dex.olf-system.svc.cluster.local:5556")])
    error_message = "Superset's settings must carry the issuer's internal_url"
  }
}
