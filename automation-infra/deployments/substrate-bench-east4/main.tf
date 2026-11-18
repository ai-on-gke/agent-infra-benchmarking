terraform {
  required_version = ">= 1.5"

  required_providers {
    github = {
      source = "integrations/github"
    }
  }

  backend "gcs" {
    bucket = "substrate-bench-east4-tfstate"
    prefix = "automation/cloud-run/substrate-bench-east4"
  }
}

locals {
  # The backend's bucket, which the backend block cannot take from here.
  state_bucket = "substrate-bench-east4-tfstate"
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

# The bare-metal cases again, as substrate-bench-metal runs them, in another
# region and at another time of day, for another chance at a free machine. Its
# name must not start with another deployment's, or that deployment's alert
# would take its failures too.
module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = "akvelon-gke-aieco"
  region                = "us-east4"
  default_resource_name = "substrate-bench-east4"
  create_jobs           = var.create_jobs

  state_bucket        = local.state_bucket
  state_bucket_prefix = "terraform"
  reports_bucket      = local.state_bucket
  reports_prefix      = "data"
  source_branch       = "main"

  # The cases scenarios.yaml's bare-metal suite lists.
  args = ["--suite=bare-metal"]

  # Daily, 12 hours from substrate-bench-metal's run.
  schedule = "0 13 * * *"
}

output "cloud_run" {
  value = module.cloud_run
}
