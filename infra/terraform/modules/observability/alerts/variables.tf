variable "namespace" {
  description = "Namespace of the evaluator CronJob (the platform namespace, olf-system). The receivers' Secrets and the S3 key Secret are read from here."
  type        = string
}

variable "release_name" {
  description = "Name of the CronJob, ServiceAccount and settings ConfigMap."
  type        = string
  default     = "alert-evaluator"
}

variable "image" {
  description = "The platform API image (ADR 0023, decision 1): the CronJob overrides its command, so no second image exists."
  type        = string
  default     = null
}

variable "image_pull_policy" {
  description = "Pull policy of the evaluator image; the local root loads it into kind and passes Never."
  type        = string
  default     = "IfNotPresent"
}

variable "stages" {
  description = "Enabled stages, each read once per evaluation."
  type        = list(string)

  validation {
    condition     = length(var.stages) > 0
    error_message = "The evaluator reads at least one stage."
  }
}

variable "dagster_graphql_urls" {
  description = "Stage to that stage's in-cluster Dagster GraphQL URL (ADR 0017 F38). Names exactly the enabled stages."
  type        = map(string)

  validation {
    condition     = toset(keys(var.dagster_graphql_urls)) == toset(var.stages)
    error_message = "dagster_graphql_urls names exactly the enabled stages; a stage with no URL could never be read."
  }
}

variable "receivers" {
  description = "One entry per webhook receiver; with none, the module creates nothing (ADR 0023: the evaluator exists only with a receiver to notify). Each names, the Secret and key holding its URL, and the stages it is limited to. The Secret is created out-of-band; only its name and key are passed, and the pod gets the value through secretKeyRef."
  type = list(object({
    name        = string
    secret_name = string
    secret_key  = string
    stages      = optional(list(string), [])
  }))

  validation {
    condition     = length(distinct([for receiver in var.receivers : receiver.name])) == length(var.receivers)
    error_message = "Receiver names are distinct."
  }
}

variable "state_base_uri" {
  description = "s3://<ops bucket>/<prefix>/ where state and history live; ends in '/'."
  type        = string

  validation {
    condition     = startswith(var.state_base_uri, "s3://") && endswith(var.state_base_uri, "/")
    error_message = "state_base_uri is s3://<bucket>/<prefix>/, ending in '/'."
  }
}

variable "s3_endpoint_url" {
  description = "S3 endpoint of the object store, from its provider contract."
  type        = string
}

variable "s3_region" {
  description = "S3 region of the object store."
  type        = string
}

variable "credentials_secret_name" {
  description = "Secret in `namespace` with AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, the key the SeaweedFS credentials Job mints for openlakeforge-alerts. Passed by reference, never as a value."
  type        = string
}

variable "ca_configmap_name" {
  description = "ConfigMap in `namespace` whose ca.crt signs the local CA's certificates (modules/access/in-cluster-resolution), mounted so a receiver behind it verifies. Null unless TLS is local-ca."
  type        = string
  default     = null
}

variable "dagster_scheme" {
  description = "Scheme of the Dagster run links: http when spec.access.tls is none, https otherwise (ADR 0023, decision 5)."
  type        = string
  default     = "https"

  validation {
    condition     = contains(["http", "https"], var.dagster_scheme)
    error_message = "dagster_scheme is http or https."
  }
}

variable "dagster_host_template" {
  description = "The stage Dagster host with a {stage} placeholder, e.g. dagster-{stage}.<base_domain>. Null without spec.access, which leaves the run link out."
  type        = string
  default     = null

  validation {
    condition     = var.dagster_host_template == null || strcontains(coalesce(var.dagster_host_template, ""), "{stage}")
    error_message = "dagster_host_template contains {stage}."
  }
}

variable "runbook_base_url" {
  description = "Base URL the notification's runbook link anchors into; null omits the link."
  type        = string
  default     = null
}
