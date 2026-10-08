variable "project_id" {
  description = "GCP project ID to deploy the Cloud Run job into."
  type        = string
}

variable "region" {
  description = "Region of the Cloud Run job and the Cloud Scheduler job."
  type        = string
  default     = "us-central1"
}

variable "default_resource_name" {
  description = "Name of every resource this config creates, unless a more specific variable sets one."
  type        = string

  validation {
    condition     = can(regex("^[a-z][-a-z0-9]{4,28}[a-z0-9]$", var.default_resource_name))
    error_message = "default_resource_name must be 6-30 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}

variable "command" {
  description = "Overrides the image's entrypoint. Empty keeps it."
  type        = list(string)
  default     = []
}

variable "args" {
  description = "Arguments passed to the entrypoint after --project-id, --region and --state-bucket, which the module passes itself."
  type        = list(string)
  default     = []
}

variable "env" {
  description = "Environment variables set in the container."
  type        = map(string)
  default     = {}
}

variable "cpu" {
  description = "CPU limit of the container."
  type        = string
  default     = "2"
}

variable "memory" {
  description = "Memory limit of the container. The container's filesystem is in memory and counts against it."
  type        = string
  default     = "4Gi"
}

variable "task_timeout" {
  description = "How long the task may run before it is stopped, as a duration in seconds such as \"86400s\"."
  type        = string
  default     = "86400s"
}

variable "max_retries" {
  description = "How many times a failed task is retried."
  type        = number
  default     = 0
}

variable "schedule" {
  description = "Cron schedule the job runs on. Null creates no Cloud Scheduler job, leaving the job to be run by hand."
  type        = string
  default     = null
}

variable "time_zone" {
  description = "Time zone the schedule is read in."
  type        = string
  default     = "Etc/UTC"
}

# Bucket names are global, hence the project ID in the default.
variable "runner_state_bucket" {
  description = "Name of the bucket holding runner/infra's Terraform state. Empty takes <project_id>-<default_resource_name>."
  type        = string
  default     = ""
}

variable "github_actions" {
  description = "GitHub repository, as owner/name, whose Actions workflows may write under source/ in bucket, through Workload Identity Federation. Null grants none."
  type = object({
    repository = string
    bucket     = string
  })
  default = null
}
