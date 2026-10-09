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
  description = "Arguments passed to the entrypoint after --project-id, --region, --state-backend, --state-bucket and --state-prefix, which the module passes itself."
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

variable "schedule_args" {
  description = "Arguments the scheduled runs pass in place of args, e.g. [\"--scenario=dummy\"]. Null passes args."
  type        = list(string)
  default     = null
}

variable "time_zone" {
  description = "Time zone the schedule is read in."
  type        = string
  default     = "Etc/UTC"
}

variable "state_bucket" {
  description = "Bucket holding the deployment's Terraform state. scenario_runner/infra's goes under state_bucket_prefix in it."
  type        = string
}

variable "state_bucket_prefix" {
  description = "Prefix in state_bucket that scenario_runner/infra's Terraform state goes under, one folder per execution. The job may only write there."
  type        = string

  validation {
    condition     = can(regex("^[^/]+(/[^/]+)*$", var.state_bucket_prefix))
    error_message = "state_bucket_prefix must not be empty, nor start or end with a slash."
  }
}

variable "github_actions_bucket" {
  description = "Bucket under whose source/ GitHub Actions may write, as the github_service_account_email account, and the job reads its source from. Null creates no account and leaves the job without source, so it fails."
  type        = string
  default     = null
}

variable "source_branch" {
  description = "Branch whose latest uploaded commit the job fetches from github_actions_bucket."
  type        = string
  default     = "main"
}
