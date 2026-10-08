output "s3_secret_key" {
  description = "A root must never re-export a module's secret value."
  value       = module.seaweedfs.s3_secret_key
  sensitive   = true
}

output "unwrapped" {
  description = "Stripping sensitivity does not make it a reference."
  value       = nonsensitive(module.polaris.root_client_secret)
}
