variable "project_id" {
  description = "GCP project ID to create the run's resources in."
  type        = string
}

variable "region" {
  description = "Region of the Artifact Registry repository."
  type        = string
  default     = "us-central1"
}

variable "default_resource_name" {
  description = "Name of every resource this config creates. Unique to one scenario-runner execution."
  type        = string

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", var.default_resource_name))
    error_message = "default_resource_name must be 1-63 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}

variable "labels" {
  description = "Labels put on every resource this config creates."
  type        = map(string)
  default     = {}
}

variable "image_writers" {
  description = "IAM members, such as serviceAccount:<email>, that may push to the repository: the account Cloud Builds of the run's images run as, when it is not the project's default."
  type        = list(string)
  default     = []
}
