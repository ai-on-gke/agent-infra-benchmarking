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
  description = "Arguments passed to the entrypoint after --project-id, --region, --state-backend, --state-bucket, --state-prefix, --image-repo and, with reports_bucket, --report-bucket and --report-prefix, which the module passes itself."
  type        = list(string)
  default     = []
}

variable "env" {
  description = "Environment variables set in the container."
  type        = map(string)
  default     = {}
}

variable "cpu" {
  description = "CPU limit of the container. Cloud Run needs at least 8 for more than 24Gi of memory."
  type        = string
  default     = "8"
}

# ko's Go module and build caches for substrate's images outgrow 4Gi.
variable "memory" {
  description = "Memory limit of the container. The container's filesystem is in memory and counts against it."
  type        = string
  default     = "32Gi"
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

variable "reports_bucket" {
  description = "Bucket the cases' reports go to, under reports_prefix, and the BigQuery dataset reads them from. Null leaves them in each case's own bucket, destroyed with its cluster, and creates no dataset."
  type        = string
  default     = null
}

variable "reports_prefix" {
  description = "Prefix in reports_bucket the reports go under, as <prefix>/<run-id>/<record>/. Each case's locust master may only write there."
  type        = string
  default     = "data"

  validation {
    condition     = can(regex("^[^/]+(/[^/]+)*$", var.reports_prefix))
    error_message = "reports_prefix must not be empty, nor start or end with a slash."
  }
}

variable "reports_writer_project_ids" {
  description = "Other projects whose clusters' benchmark-runner KSA may write the reports too, as the cluster of a local or dev run in another project."
  type        = list(string)
  default     = []
}

variable "github_actions" {
  description = "Whether GitHub Actions of github_repository may push the runner image and the runs' images, as the github_service_account_email account. False creates no account, so no workflow can push the image the job runs."
  type        = bool
  default     = true
}

variable "source_branch" {
  description = "Branch whose latest commit's runner image the job runs, as .github/workflows/publish.yml tags it."
  type        = string
  default     = "main"
}

variable "runner_image" {
  description = "Image both jobs run in place of source_branch's runner image, e.g. one commit's <repository>/test-runner:<commit>. Null runs source_branch's."
  type        = string
  default     = null
}

variable "create_jobs" {
  description = "Whether to create the job, its cleanup job and their schedulers. False for a new deployment's first apply, before the workflow has pushed the runner image they run."
  type        = bool
  default     = true
}

variable "cleanup_schedule" {
  description = "Cron schedule automation-infra/tools/destroy-leftovers runs on, in the job's image, to destroy what killed executions left. Null creates no cleanup job; only with a schedule."
  type        = string
  default     = "0 15 * * *"
}

variable "network_cidr" {
  description = "Primary range of the runs' subnetwork: their nodes, their control planes' internal endpoints and the job's own addresses. GKE adds each cluster's Pod and Service ranges beside it."
  type        = string
  default     = "10.100.0.0/20"
}

variable "alert_emails" {
  description = "Addresses told of a failed execution of the job or its cleanup. Empty still records the incidents in Cloud Monitoring."
  type        = list(string)
  default     = []
}

variable "github_repository" {
  description = "GitHub repository, owner/name, whose Actions may push the images as the github_service_account_email account, through Workload Identity Federation rather than a key, and which gets the variables they authenticate with. Its owner must be the github provider's."
  type        = string
  default     = "ai-on-gke/agent-infra-benchmarking"
}
