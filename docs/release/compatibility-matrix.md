<!--
This file is generated from release/component-catalog.yaml. Do not hand-edit
the tables below -- regenerate with:

    olf release compatibility-matrix --output docs/release/compatibility-matrix.md

(or `uv run --project tools/olf olf release compatibility-matrix --output ...`
from the repo root). The same command produces the copy embedded in every
release bundle by .github/workflows/release.yml, so this file always matches
what a tagged release publishes as of the last catalog update.
`olf check all` fails if this checked-in file drifts from a fresh render;
regenerate it whenever release/component-catalog.yaml changes.
-->

# OpenLakeForge 0.4.0-alpha.1 compatibility matrix

Generated from `release/component-catalog.yaml`. Every version below is the exact input pinned for this release; see [docs/release/component-catalog.md](component-catalog.md) for the update process.

## Platform

| Component | Required version |
| --- | --- |
| Terraform | >= 1.10.0 |

## Terraform providers

The tracked/approved version for each provider. Individual Terraform roots can lock an older compatible version (below) -- consult that table for the exact version actually applied to a given target.

| Provider | Tracked version |
| --- | --- |
| hashicorp/aws | 6.62.0 |
| hashicorp/azurerm | 4.77.0 |
| hashicorp/helm | 3.2.0 |
| hashicorp/kubernetes | 2.38.0 |
| hashicorp/random | 3.9.0 |
| hashicorp/tls | 4.3.0 |

### Terraform providers by root

The exact version `.terraform.lock.hcl` pins for each root -- what a consumer of that target actually gets, not the tracked version above.

| Provider | infra/terraform/environments/aws-poc/.terraform.lock.hcl | infra/terraform/environments/azure-poc/.terraform.lock.hcl | infra/terraform/environments/local/.terraform.lock.hcl | infra/terraform/foundations/aws-eks/.terraform.lock.hcl | infra/terraform/foundations/azure-aks/.terraform.lock.hcl |
| --- | --- | --- | --- | --- | --- |
| hashicorp/aws | 6.62.0 |  |  | 6.62.0 |  |
| hashicorp/azurerm |  |  |  |  | 4.77.0 |
| hashicorp/helm | 3.2.0 | 3.2.0 | 3.1.1 |  |  |
| hashicorp/kubernetes | 2.38.0 | 2.38.0 | 2.38.0 |  |  |
| hashicorp/random | 3.9.0 | 3.9.0 | 3.9.0 |  | 3.9.0 |
| hashicorp/tls |  |  |  | 4.3.0 |  |

## Helm charts

| Chart | Version |
| --- | --- |
| cert-manager | v1.21.2 |
| dagster | 1.13.7 |
| openmetadata | 1.13.6 |
| openmetadata-dependencies | 1.13.6 |
| polaris | 1.4.1 |
| seaweedfs | 4.23.0 |
| superset | 0.15.5 |
| traefik | 41.6.1 |
| trino | 1.42.2 |

## Managed toolchain

Terraform, Helm, kubectl, and kind are provisioned by `olf toolchain` (#127) rather than installed by the consumer; versions below are what the current release provisions.

| Tool | Version |
| --- | --- |
| helm | 3.18.6 |
| kind | 0.32.0 |
| kubectl | 1.31.4 |
| terraform | 1.16.4 |

## Container images

| Image | Reference |
| --- | --- |
| cert_manager_acmesolver | `quay.io/jetstack/cert-manager-acmesolver:v1.21.2@sha256:699b40d622211ab7accad8a21b04c5fbaa1841ef7a12621e8de492dbe27b2503` |
| cert_manager_cainjector | `quay.io/jetstack/cert-manager-cainjector:v1.21.2@sha256:c85268c64f2e0e76684bf5fe8906caff34b82523561c6affe0fae3546bd87562` |
| cert_manager_controller | `quay.io/jetstack/cert-manager-controller:v1.21.2@sha256:70f532fd9cfde0b09d55687200942399d89838bc2d5d5b45152eb799a15912b8` |
| cert_manager_startupapicheck | `quay.io/jetstack/cert-manager-startupapicheck:v1.21.2@sha256:46e75b6866359ffb5d82624f41e3ed1c70b2994982702ced547ce5edb418a8f5` |
| cert_manager_webhook | `quay.io/jetstack/cert-manager-webhook:v1.21.2@sha256:a60e2dac46dbb8a7f3df95c54ce941012f54c2fe022f0ee55aaa1ab40ed957ae` |
| dagster_control_plane | `docker.io/dagster/dagster-celery-k8s:1.13.7@sha256:7e9fa5d3f9724bdf382932f34294dbfe5c9ca550641795dff793d6b59f3cc4ee` |
| e2e_mail_sink | `axllent/mailpit:v1.31.1@sha256:98b916bd3c8d61f7633a52d3ea2f58d00620cb01ca57ab59edde68c347a95365` |
| k8s_bootstrap | `alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e` |
| keycloak | `quay.io/keycloak/keycloak:26.6.4@sha256:0aae0de7fca85525f727d3354df17896092de8bb26ae4c12d89c77e5df8cbce4` |
| keycloak_config_cli | `adorsys/keycloak-config-cli:6.5.1-26.5.5@sha256:0955d98c8a341898b7aa177477edf8a1e90569ae50bbe7598141c1270b773274` |
| openmetadata_ingestion | `docker.getcollate.io/openmetadata/ingestion-base:1.13.6@sha256:29f8dcafc52bdbdb60dc3901569d9cd752cd10cb942375f5e08b53d38b05239e` |
| opensearch | `opensearchproject/opensearch:3.3.2@sha256:798cf28e226a32f5c928dd1ed9478dd3a33d2212176aad3679020088ad3afa1a` |
| polaris | `apache/polaris:1.4.0@sha256:ef4947a3fd005ca5b2aec2bde98682a59996d38f21c16c4660fbb79e4c20b40c` |
| polaris_admin_tool | `apache/polaris-admin-tool:1.4.0@sha256:7ef7557b528964e792caeaef3908434bd99c7d2f994caa654da1d77c6b428a80` |
| portal_static_server | `busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e` |
| postgres | `postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777` |
| project_code_base | `python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de` |
| seaweedfs | `chrislusf/seaweedfs:4.23@sha256:c6d6fb84b081f1f09bb089184ff4b45d2f163a1bfa8b354d04cf400c6e06f242` |
| superset_base | `apache/superset:6.1.0@sha256:fb3464528ec7076f91195f0ff7835755aa023e281f1bb78a84782ce7a36b3705` |
| superset_init | `apache/superset:dockerize@sha256:afe59523a6c8774c3b16d0f44146b2e52f327a7d26a47b4cc63b904fcdedf057` |
| superset_redis | `docker.io/bitnamilegacy/redis:7.0.10-debian-11-r4@sha256:224a79826b42869bdc72a70933efd840c5a5f10a70caafca68e57be6901e36fb` |
| traefik | `docker.io/traefik:v3.7.13@sha256:24841fe2de7304c149343d877d2923b4c8800a38ba015dea9174c23b20e344a0` |
| trino | `trinodb/trino:480@sha256:1565e8cac299a32dd9177a4da2d748da4ceb9f1560a9c409d1d18fd72ea5253e` |

## Cloud services (deployment targets)

| Target | Kubernetes foundation | Object storage | Catalog | Managed database |
| --- | --- | --- | --- | --- |
| Local | kind | SeaweedFS (in-cluster) | Polaris (in-cluster) | PostgreSQL (in-cluster) |
| Azure POC | AKS | SeaweedFS (in-cluster) | Polaris (in-cluster) | PostgreSQL (in-cluster) |
| AWS POC | EKS | S3 | AWS Glue | RDS PostgreSQL |

## Supported upgrade paths

OpenLakeForge is in the Alpha lifecycle stage (see [releasing.md](releasing.md#lifecycle-stages)): breaking changes are allowed between alpha releases, with migration notes published in `CHANGELOG.md` for every tag. Until Beta, only the latest alpha tag is maintained; there is no supported upgrade path guarantee prior to `v0.1.0-alpha.1`.
