# Trino's HTTP server intermittently cut a compressed response after its gzip
# header, which reached `olf query` as ChunkedEncodingError (#1088), so every
# Trino node serves responses uncompressed.

mock_provider "helm" {}
mock_provider "kubernetes" {}

variables {
  namespace        = "olf-dev"
  base_values_file = "tests/values.yaml"
  storage_contract = {
    endpoint                = "http://seaweedfs-s3.olf-system:8333"
    region                  = "us-east-1"
    path_style_access       = true
    credentials_secret_name = "seaweedfs-s3-credentials"
  }
  catalog_contract = {}
  stage_catalog_contracts = {
    dev = {
      catalog_name                  = "lakehouse_dev"
      catalog_provider              = "polaris"
      rest_uri                      = "http://polaris.olf-system:8181/api/catalog"
      token_uri                     = "http://polaris.olf-system:8181/api/catalog/v1/oauth/tokens"
      warehouse                     = "lakehouse_dev"
      oauth_scope                   = "PRINCIPAL_ROLE:ALL"
      trino_credentials_secret_name = "polaris-dev-trino-catalog-creds"
    }
  }
}

run "responses_are_served_uncompressed" {
  command = plan

  assert {
    condition = contains(
      yamldecode(helm_release.trino.values[length(helm_release.trino.values) - 1]).additionalConfigProperties,
      "http-server.compression.enabled=false",
    )
    error_message = "Trino's HTTP server must not compress responses (#1088)"
  }
}
