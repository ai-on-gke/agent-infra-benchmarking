variable "project_id" {
  description = "GCP project ID to create the run's resources in."
  type        = string
}

variable "region" {
  description = "Region of the Artifact Registry repository."
  type        = string
  default     = "us-central1"
}

# One run_tests.py execution's resources, so it has to differ from every
# other execution's that may exist at the same time.
variable "name" {
  description = "Name of the run's resources: the Artifact Registry repository's ID."
  type        = string

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", var.name))
    error_message = "name must be 1-63 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}
