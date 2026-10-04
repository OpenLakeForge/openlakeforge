output "storage_contract" {
  description = "Non-secret S3 storage contract consumed by platform modules."
  value       = local.storage_contract
}

output "foundation_contract" {
  description = "Provider-neutral foundation contract consumed by the AWS POC platform."
  value       = local.foundation_contract
}

output "kubernetes_platform_contract" {
  description = "Provider-neutral Kubernetes platform contract consumed by AWS POC modules and scripts."
  value       = local.kubernetes_platform_contract
}

output "project_code_ecr_repository_url" {
  description = "AWS ECR repository URL used for project-code images."
  value       = try(local.foundation_contract.ecr_project_code_repository_url, null)
}

output "superset_ecr_repository_url" {
  description = "AWS ECR repository URL used for Superset images."
  value       = try(local.foundation_contract.ecr_superset_repository_url, null)
}

output "floe_manifest_base_uri" {
  description = "S3 base URI of domain Floe manifests consumed by Dagster and Floe."
  value       = local.floe_manifest_base_uri
}

output "domain_floe_manifest_uris" {
  description = "S3 URIs of domain Floe manifests consumed by Dagster and Floe."
  value       = local.domain_floe_manifest_uris
}

output "catalog_contract" {
  description = "Non-secret Iceberg catalog contract consumed by platform modules."
  value       = local.catalog_contract
}

output "metadata_database_contract" {
  description = "Non-secret metadata database contract consumed by platform modules."
  value       = local.metadata_database_contract
}

output "superset_contract" {
  description = "Non-secret Superset reporting contract."
  value       = local.reporting_contract
}

output "artifact_registry_contract" {
  description = "Runtime image registry/distribution contract."
  value       = local.artifact_registry_contract
}

output "artifact_bucket_contract" {
  description = "Object-storage artifact bucket contract."
  value       = local.artifact_bucket_contract
}

output "secrets_contract" {
  description = "Secret reference and delivery contract."
  value       = local.secrets_contract
}

output "identity_contract" {
  description = "Identity and workload identity contract."
  value       = local.identity_contract
}

output "access_contract" {
  description = "Internal and external service access contract."
  value       = local.access_contract
}

output "provider_contracts" {
  description = "Provider-neutral contract map for the AWS POC implementation."
  value       = local.provider_contracts
}

output "shared_namespace" {
  description = "Namespace owning the shared platform services."
  value       = kubernetes_namespace_v1.shared.metadata[0].name
}

output "stage_names" {
  description = "Stages this root has provisioned. `olf deploy` compares it against the resolved topology to refuse an apply that would silently remove one."
  value       = sort(keys(local.enabled_stages))
}

output "stage_namespaces" {
  description = "Namespace owning each enabled stage's services."
  value       = local.stage_namespaces
}

output "stage_service_accounts" {
  description = "Runtime identity per enabled stage: the Dagster Helm release's own service account, bound to that stage's IAM role via EKS Pod Identity."
  value       = local.stage_service_accounts
}
