output "policy_names" {
  description = "Stage name to the allow policy applied in that stage namespace."
  value       = { for stage, policy in kubernetes_network_policy_v1.allow : stage => policy.metadata[0].name }
}

output "workload_policy_names" {
  description = "Workload name to the policy applied in the shared namespace."
  value       = { for name, policy in kubernetes_network_policy_v1.workload : name => policy.metadata[0].name }
}
