variable "namespace" {
  description = "Kubernetes namespace where the platform API is deployed."
  type        = string
}

variable "release_name" {
  description = "Name of the platform API Deployment, Service and ConfigMap."
  type        = string
  default     = "platform-api"
}

# No default: no release has published the image, so there is no digest to pin
# here (images/platform-api/README.md). The root passes the image olf built.
variable "image" {
  description = "platform-api image reference: the local build olf loaded into kind until a release pins one in release/component-catalog.yaml."
  type        = string
}

variable "image_pull_policy" {
  description = "Pull policy for the image; Never for an image olf loaded into kind."
  type        = string
  default     = "IfNotPresent"
}

variable "issuer_url" {
  description = "identity.oidc issuer_url: the issuer whose tokens the API validates."
  type        = string
}

variable "audiences" {
  description = "Client ids whose tokens the API accepts (ADR 0021, decision 3). At least one."
  type        = list(string)

  validation {
    condition     = length(var.audiences) > 0 && alltrue([for audience in var.audiences : audience != ""])
    error_message = "audiences must list at least one non-empty client id; an empty list would accept any audience."
  }
}

variable "role_claim" {
  description = "identity.oidc role_claim: the token claim holding the role values."
  type        = string
}

variable "role_mapping" {
  description = "identity.oidc role_mapping: canonical role to the claim values that carry it."
  type        = map(list(string))
}

variable "precedence" {
  description = "Canonical roles, highest first (release/identity-roles.yaml)."
  type        = list(string)
}

variable "stages" {
  description = "Enabled stage names, the only stages a stage-scoped operation answers for."
  type        = list(string)
}

variable "ca_configmap_name" {
  description = "ConfigMap in `namespace` whose ca.crt signs the issuer's certificate (modules/access/in-cluster-resolution). Null when the issuer's certificate chains to a public root."
  type        = string
  default     = null
}

variable "dagster_graphql_urls" {
  description = "Each enabled stage's Dagster webserver GraphQL URL, keyed by stage. The API refuses to start on a key outside `stages`."
  type        = map(string)
  default     = {}
}

variable "openmetadata_url" {
  description = "OpenMetadata's in-cluster base URL, which the API forwards each caller's token to. Null when governance is not deployed."
  type        = string
  default     = null
}

variable "log_archives" {
  description = "Each enabled stage's log archive: the base URI its archiver writes under and the namespace it archives. The API refuses to start on a stage outside `stages`."
  type = map(object({
    base_uri  = string
    namespace = string
  }))
  default = {}
}

variable "s3_endpoint_url" {
  description = "The object store the log archives are read from; null for AWS S3."
  type        = string
  default     = null
}

variable "s3_region" {
  description = "Region the S3 client signs for."
  type        = string
  default     = "us-east-1"
}

variable "log_reader_secret_name" {
  description = "Secret in `namespace` holding the read-only object-store key (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) for the log prefixes. Null leaves the logs route without a key."
  type        = string
  default     = null
}
