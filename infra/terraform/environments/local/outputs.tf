output "storage_contract" {
  description = "Non-secret S3-compatible storage contract consumed by platform modules."
  value       = local.storage_contract
}

output "foundation_contract" {
  description = "Provider-neutral foundation contract consumed by the local platform."
  value       = local.foundation_contract
}

output "kubernetes_platform_contract" {
  description = "Provider-neutral Kubernetes platform contract consumed by local modules and scripts."
  value       = local.kubernetes_platform_contract
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
  description = "Provider-neutral contract map for the local implementation."
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
  description = "Runtime identity per enabled stage, for the storage and catalog permissions #114 binds to it."
  value       = { for name, account in kubernetes_service_account_v1.stage_runtime : name => account.metadata[0].name }
}
