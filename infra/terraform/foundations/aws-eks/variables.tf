variable "aws_region" {
  description = "AWS region for the EKS POC foundation."
  type        = string
  default     = "eu-west-1"
}

variable "cluster_name" {
  description = "EKS cluster name. olf always passes it, from AWS_CLUSTER_NAME, the name the existing state records, or eks-openlakeforge."
  type        = string
  default     = "eks-openlakeforge-poc"
}

variable "kubeconfig_path" {
  description = "Kubeconfig path populated by aws eks update-kubeconfig. Defaults to the repository-local .tmp/kubeconfigs/aws.yaml."
  type        = string
  default     = null
}

variable "default_tags" {
  description = "Tags applied to every taggable resource via the provider default_tags block. Account-mandated tags (Project/Owner/Requester/Env/IaC) are supplied via a .tfvars file. Casing is significant."
  type        = map(string)
  default     = {}
}

variable "kubernetes_version" {
  description = "Optional EKS Kubernetes version. Null uses the AWS default supported by EKS."
  type        = string
  default     = null
}

variable "vpc_cidr" {
  description = "CIDR block for the POC VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_access_cidrs" {
  description = "CIDR blocks allowed to reach the public EKS API endpoint."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "node_desired_size" {
  description = "Desired EKS managed node group size."
  type        = number
  default     = 3
}

variable "node_min_size" {
  description = "Minimum EKS managed node group size."
  type        = number
  default     = 1
}

variable "node_max_size" {
  description = "Maximum EKS managed node group size."
  type        = number
  default     = 4
}

variable "node_instance_types" {
  description = "EC2 instance types for the default EKS node group."
  type        = list(string)
  default     = ["m7i.large"]
}

variable "project_code_ecr_repository_name" {
  description = "ECR repository name for the project-code runtime image."
  type        = string
  default     = "openlakeforge/project-code"
}

variable "superset_ecr_repository_name" {
  description = "ECR repository name for the custom Superset runtime image."
  type        = string
  default     = "openlakeforge/superset"
}
