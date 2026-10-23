terraform {
  required_version = ">= 1.5"

  required_providers {
    github = {
      source = "integrations/github"
    }
  }

  backend "gcs" {
    bucket = "substrate-bench-staging-tfstate"
    prefix = "automation/cloud-run/substrate-bench-staging"
  }
}

locals {
  # The backend's bucket, which the backend block cannot take from here.
  state_bucket = "substrate-bench-staging-tfstate"
}

variable "create_jobs" {
  description = "False for the first apply, before the workflow has pushed the runner image."
  type        = bool
  default     = true
}

# Authenticates as gh auth token, or GITHUB_TOKEN, which may set the repository's
# Actions variables.
provider "github" {
  owner = "ai-on-gke"
}

# substrate-bench-auto's twin, in the same project, for trying a change on a
# branch before it reaches main. Its name must not start with
# substrate-bench-auto, or that deployment's alert would take its failures too.
module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = "akvelon-gke-aieco"
  region                = "us-central1"
  default_resource_name = "substrate-bench-staging"
  create_jobs           = var.create_jobs

  state_bucket        = local.state_bucket
  state_bucket_prefix = "terraform"
  reports_bucket      = local.state_bucket
  reports_prefix      = "data"
  source_branch       = "main"

  # The cases scenarios.yaml's staging suite lists.
  args = ["--suite=staging"]

  # Daily, an hour after substrate-bench-auto's.
  schedule = "0 4 * * *"
}

output "cloud_run" {
  value = module.cloud_run
}
