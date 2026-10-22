terraform {
  required_version = ">= 1.5"

  required_providers {
    github = {
      source = "integrations/github"
    }
  }

  backend "gcs" {
    bucket = "substrate-bench-auto-tfstate"
    prefix = "automation/cloud-run/substrate-bench-auto"
  }
}

locals {
  # The backend's bucket, which the backend block cannot take from here.
  state_bucket = "substrate-bench-auto-tfstate"
}

variable "create_jobs" {
  description = "False while the workflow has not pushed the runner image yet, which destroys the jobs and their schedules until the next apply with true."
  type        = bool
  default     = true
}

# Authenticates as gh auth token, or GITHUB_TOKEN, which may set the repository's
# Actions variables.
provider "github" {
  owner = "ai-on-gke"
}

module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = "akvelon-gke-aieco"
  region                = "us-central1"
  default_resource_name = "substrate-bench-auto"
  create_jobs           = var.create_jobs

  state_bucket        = local.state_bucket
  state_bucket_prefix = "terraform"
  reports_bucket      = local.state_bucket
  reports_prefix      = "data"
  source_branch       = "main"

  # The cases the nightly tracks, each repeat on a cluster of its own; named
  # rather than --all, so a case added to scenarios.yaml runs here only once it
  # is listed.
  args = [
    "--scenario=glutton",
    "--scenario=glutton-microvm",
    "--scenario=openclaw",
    "--scenario=openclaw-microvm",
    "--scenario=dummy",
  ]

  # Daily, so an execution ends before the next starts rather than running
  # beside it.
  schedule = "0 3 * * *"
}

output "cloud_run" {
  value = module.cloud_run
}
