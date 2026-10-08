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
  description = "Name of every resource this config creates. Unique to one run.py execution."
  type        = string

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", var.default_resource_name))
    error_message = "default_resource_name must be 1-63 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}
