# A keyless catalog with no static key falls back to the AWS SDK default chain
# (environment, IRSA, instance profile) for anything outside vended credentials
# (#963), so the coordinator must carry none of them (ADR 0019 section 9). The
# instance metadata endpoint is closed by the stage's default-deny policy:
# tools/olf/tests/infra/test_trino_ambient_credentials.py.

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

run "the_coordinator_carries_no_ambient_cloud_credential" {
  command = plan

  assert {
    condition = alltrue(flatten([
      for doc in helm_release.trino.values : [
        for entry in try(yamldecode(doc).env, []) : length(regexall("^(AWS_|OPENLAKEFORGE_S3_)", entry.name)) == 0
      ]
    ]))
    error_message = "the Trino pods must have no AWS or storage key in their environment"
  }

  assert {
    condition = alltrue([
      for doc in helm_release.trino.values : length(try(yamldecode(doc).serviceAccount.annotations, {})) == 0
    ])
    error_message = "the Trino service account must carry no role annotation"
  }
}

run "every_rest_catalog_is_keyless" {
  command = plan

  assert {
    condition = alltrue(flatten([
      for doc in helm_release.trino.values : [
        for properties in values(try(yamldecode(doc).catalogs, {})) : [
          strcontains(properties, "iceberg.rest-catalog.session=USER"),
          strcontains(properties, "iceberg.rest-catalog.vended-credentials-enabled=true"),
          strcontains(properties, "iceberg.rest-catalog.oauth2.token-refresh-enabled=false"),
          !strcontains(properties, "s3.aws-access-key"),
          !strcontains(properties, "s3.aws-secret-key"),
        ]
      ]
    ]))
    error_message = "every REST catalog must pass the caller's token to Polaris and read only vended credentials"
  }

  assert {
    condition = toset(flatten([
      for doc in helm_release.trino.values : keys(try(yamldecode(doc).catalogs, {}))
    ])) == toset(["lakehouse_dev"])
    error_message = "the stage serves one catalog, under the contract's catalog_name"
  }

  assert {
    condition = alltrue([
      for doc in helm_release.trino.values : !can(yamldecode(doc).accessControl)
    ])
    error_message = "Polaris decides every access to a keyless catalog: Trino carries no rules"
  }
}
