output "aws_region" {
  description = "AWS region."
  value       = var.aws_region
}

output "aws_account_id" {
  description = "AWS account ID."
  value       = data.aws_caller_identity.current.account_id
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.this.name
}

output "kube_context" {
  description = "Expected kubeconfig context after aws eks update-kubeconfig --alias."
  value       = aws_eks_cluster.this.name
}

output "kubeconfig_path" {
  description = "Kubeconfig path used by platform Terraform."
  value       = local.kubeconfig_path
}

output "vpc_id" {
  description = "POC VPC ID."
  value       = aws_vpc.this.id
}

output "vpc_cidr_block" {
  description = "POC VPC CIDR block."
  value       = aws_vpc.this.cidr_block
}

output "subnet_ids" {
  description = "EKS subnet IDs."
  value       = values(aws_subnet.public)[*].id
}

output "oidc_issuer_url" {
  description = "EKS OIDC issuer URL (informational; workload identity uses Pod Identity, not IRSA)."
  value       = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

output "workload_identity_type" {
  description = "Workload identity mechanism bound to service accounts."
  value       = "eks-pod-identity"
}

output "project_code_ecr_repository_url" {
  description = "ECR repository URL for the project-code runtime image."
  value       = aws_ecr_repository.project_code.repository_url
}

output "foundation_contract" {
  description = "Provider-neutral AWS EKS foundation contract."
  value = {
    provider                        = "aws"
    implementation                  = "eks"
    adapter                         = "foundation.eks"
    cluster_name                    = aws_eks_cluster.this.name
    kube_context                    = aws_eks_cluster.this.name
    kubeconfig_path                 = local.kubeconfig_path
    aws_region                      = var.aws_region
    aws_account_id                  = data.aws_caller_identity.current.account_id
    cluster_type                    = "eks"
    vpc_id                          = aws_vpc.this.id
    vpc_cidr_block                  = aws_vpc.this.cidr_block
    subnet_ids                      = values(aws_subnet.public)[*].id
    oidc_issuer_enabled             = true
    oidc_issuer_url                 = aws_eks_cluster.this.identity[0].oidc[0].issuer
    workload_identity_enabled       = true
    workload_identity_type          = "eks-pod-identity"
    ecr_project_code_repository_url = aws_ecr_repository.project_code.repository_url
  }
}
