<!--
This file is generated from release/component-catalog.yaml. Do not hand-edit
the tables below -- regenerate with:

    olf release compatibility-matrix --output docs/reference/compatibility-matrix.md

(or `uv run --project tools/olf olf release compatibility-matrix --output ...`
from the repo root). The same command produces the copy embedded in every
release bundle by .github/workflows/release.yml, so this file always matches
what a tagged release publishes as of the last catalog update.
`olf check all` fails if this checked-in file drifts from a fresh render;
regenerate it whenever release/component-catalog.yaml changes.
-->

# OpenLakeForge 0.6.0-alpha.3 compatibility matrix

Generated from `release/component-catalog.yaml`. Every version below is the exact input pinned for this release; see [docs/reference/component-catalog.md](component-catalog.md) for the update process.

## Platform

| Component | Required version |
| --- | --- |
| Terraform | >= 1.10.0 |

## Project revision runtime contract

A ProjectRevision built for any accepted contract version activates on this distribution without a rebuild, whatever distribution version built it. A revision outside the range must be rebuilt (ADR 0012).

| Current contract | Accepted contracts |
| --- | --- |
| 1 | 1 |

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
| cert-manager | v1.21.3 |
| dagster | 1.13.7 |
| openmetadata | 1.13.6 |
| openmetadata-dependencies | 1.13.6 |
| polaris | 1.9.0 |
| seaweedfs | 4.54.0 |
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
| cert_manager_acmesolver | `quay.io/jetstack/cert-manager-acmesolver:v1.21.3@sha256:c8c1225bb2fe8ab37a330b77f0d8c2ee6a41210fcb54fc07ca606ab9a022b8b7` |
| cert_manager_cainjector | `quay.io/jetstack/cert-manager-cainjector:v1.21.3@sha256:083d0e906f8eb44e255fe4ff817ae54e49f9ed82e22f1011bb4e9e4f4b8a641f` |
| cert_manager_controller | `quay.io/jetstack/cert-manager-controller:v1.21.3@sha256:9f4f96a33c7774cef24466fcd85031997531a03a8d117f151126baa8d1290dcc` |
| cert_manager_startupapicheck | `quay.io/jetstack/cert-manager-startupapicheck:v1.21.3@sha256:1c519d4afca1413d1e4ccd78bdc756184418ab55a34569a3b3de39d32f9974e1` |
| cert_manager_webhook | `quay.io/jetstack/cert-manager-webhook:v1.21.3@sha256:055ac8e9de6a0ba6763794838fe38944acc650f68ebe1b01dd8be079e580f458` |
| dagster_busybox | `docker.io/busybox:1.28@sha256:141c253bc4c3fd0a201d32dc1f493bcf3fff003b6df416dea4f41046e0f37d47` |
| dagster_control_plane | `docker.io/dagster/dagster-celery-k8s:1.13.7@sha256:7e9fa5d3f9724bdf382932f34294dbfe5c9ca550641795dff793d6b59f3cc4ee` |
| dex | `ghcr.io/dexidp/dex:v2.46.1@sha256:65b72ca0f4b0bf6d4ada84ec21da8851782563404ac7b8f926923d27ceced9ab` |
| fluent_bit | `fluent/fluent-bit:5.2.0@sha256:e2718c47aaab7d99587fdffe78818549bdd09dee5e30a9199d2ff3da6e2c7628` |
| k8s_bootstrap | `alpine/k8s:1.31.13@sha256:7a319b15cfc99c9aaa27ec9891d6b4ca24632b795908591c7b0ba89b03549e0a` |
| kube_state_metrics | `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0@sha256:42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b` |
| oauth2_proxy | `quay.io/oauth2-proxy/oauth2-proxy:v7.15.6@sha256:c3415e8660af553f5e18f5d90db050614c02493c0ac6260ac59442f6d9b12048` |
| openmetadata_ingestion | `docker.io/openmetadata/ingestion-base:1.13.6@sha256:29f8dcafc52bdbdb60dc3901569d9cd752cd10cb942375f5e08b53d38b05239e` |
| opensearch | `opensearchproject/opensearch:3.3.2@sha256:798cf28e226a32f5c928dd1ed9478dd3a33d2212176aad3679020088ad3afa1a` |
| polaris | `apache/polaris:1.9.0@sha256:bc548500f5ba8e798b7f8e107ad0b8fbd5b5c6d0c5437f56c5ca101219b796d0` |
| polaris_admin_tool | `apache/polaris-admin-tool:1.9.0@sha256:9ec2467615132f6a837b13fcbb8be23d770c0ffa5a8bb90faf8ebdf513ab14a0` |
| portal | `ghcr.io/openlakeforge/openlakeforge-portal:0.7.4@sha256:3ef9c4ca50feeecad6c028eb1fde138d0b764194a04f5d6f83e557c5e1a35043` |
| postgres | `postgres:18.7-alpine@sha256:0115267ab5a509022df9319c77be365cef335adb71556c75f60f5360ee247e71` |
| project_code_base | `python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b` |
| prometheus | `prom/prometheus:v3.13.5@sha256:aafa9d694e6a883c02db516d9f6e5b84e14195f49a2a6456a4cca815edda72b9` |
| seaweedfs | `chrislusf/seaweedfs:4.54@sha256:4b42c212543a0acd4c0e675215ecdcaf62a186ef98def76c7e12701871222d15` |
| superset | `ghcr.io/openlakeforge/openlakeforge/superset:0.5.1-alpha.3@sha256:e884c3ee1c3fa9ad78685a6867b9b809a125661df76b65f19dc17e2057b3698b` |
| superset_base | `apache/superset:6.1.0@sha256:fb3464528ec7076f91195f0ff7835755aa023e281f1bb78a84782ce7a36b3705` |
| superset_init | `apache/superset:dockerize@sha256:afe59523a6c8774c3b16d0f44146b2e52f327a7d26a47b4cc63b904fcdedf057` |
| superset_redis | `docker.io/redis:7.4.12-bookworm@sha256:2e3eb11462c4e3759e6972678dd051d150ff54b1e60714b3c33b701260172a90` |
| traefik | `docker.io/traefik:v3.7.19@sha256:a6c0194dda77bcb804824580f36fa7edc56e315b1acfe933366c8b52844a78a4` |
| trino | `trinodb/trino:480@sha256:1565e8cac299a32dd9177a4da2d748da4ceb9f1560a9c409d1d18fd72ea5253e` |

## Cloud services (deployment targets)

| Target | Kubernetes foundation | Object storage | Catalog | Managed database |
| --- | --- | --- | --- | --- |
| Local | kind | SeaweedFS (in-cluster), STS on | Polaris (in-cluster) | PostgreSQL (in-cluster) |
| Azure POC (preview) | AKS | SeaweedFS (in-cluster), STS on | Polaris (in-cluster) | PostgreSQL (in-cluster) |
| AWS POC (preview) | EKS | S3 | AWS Glue | RDS PostgreSQL |

A Polaris catalog reads only with the credentials Polaris vends through the object store's STS, so its store must offer one (provider contract `stages.<stage>.storage.sts_endpoint`). A generic S3-compatible store (#803) and AWS on Polaris (#681) are not qualified yet.

## Supported upgrade paths

OpenLakeForge is in the Alpha lifecycle stage (see [releasing.md](../release/releasing.md#lifecycle-stages)): breaking changes are allowed between alpha releases, with migration notes published in `CHANGELOG.md` for every tag. Until Beta, only the latest alpha tag is maintained; there is no supported upgrade path guarantee prior to `v0.1.0-alpha.1`.

Platform state written before v0.3 is not supported: `olf` refuses its schema `2.0.0` provider contract. Upgrade from state written by v0.3 or later. Older state keeps its shared services in the `lakehouse` namespace, which the platform apply refuses to replace: migrate the SeaweedFS, PostgreSQL and Polaris data into the current shared namespace by hand, then run `olf infra deploy --phase platform`, or run it with `--allow-stage-removal` to start over and lose that data.
