variable "project_id" {
  description = "GCP project ID of the BigQuery dataset. Its clusters' benchmark-runner KSA may write the reports."
  type        = string
}

variable "region" {
  description = "Location of the bucket the module creates, and so of the dataset. Unused with bucket."
  type        = string
  default     = "us-central1"
}

variable "name" {
  description = "Name of the deployment: the bucket the module creates is <name>-reports, and the dataset <name> with underscores for hyphens."
  type        = string

  validation {
    condition     = can(regex("^[a-z][-a-z0-9]{4,28}[a-z0-9]$", var.name))
    error_message = "name must be 6-30 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}

variable "bucket" {
  description = "Existing bucket the reports go to, in place of the one the module creates. The scenario-runner's --report-deployment does not find it."
  type        = string
  default     = null
}

variable "prefix" {
  description = "Prefix in the bucket the reports go under, as <prefix>/<run-id>/<record>/. Each case's benchmark-runner KSA may only write there. The scenario-runner's --report-deployment expects the default."
  type        = string
  default     = "data"

  validation {
    condition     = can(regex("^[^/]+(/[^/]+)*$", var.prefix))
    error_message = "prefix must not be empty, nor start or end with a slash."
  }
}

variable "writer_project_ids" {
  description = "Other projects whose clusters' benchmark-runner KSA may write the reports too, as the cluster of a local or dev run in another project."
  type        = list(string)
  default     = []
}
