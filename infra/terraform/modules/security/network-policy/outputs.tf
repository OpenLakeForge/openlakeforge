output "policy_names" {
  description = "Stage name to the allow policy applied in that stage namespace."
  value       = { for stage, policy in kubernetes_network_policy_v1.allow : stage => policy.metadata[0].name }
}

output "workload_policy_names" {
  description = "Workload name to the policy applied in the shared namespace."
  value       = { for name, policy in kubernetes_network_policy_v1.workload : name => policy.metadata[0].name }
}

output "stage_pod_policy_names" {
  description = "Stage and flow key (`<stage>/<id>-<n>`) to the pod-narrowed egress policy applied in that stage namespace."
  value       = { for key, policy in kubernetes_network_policy_v1.stage_pods : key => policy.metadata[0].name }
}
