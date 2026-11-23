terraform {
  required_version = ">= 1.5"

  required_providers {
    github = {
      source = "integrations/github"
    }
  }

  backend "gcs" {
    bucket = "substrate-bench-europe-tfstate"
    prefix = "automation/cloud-run/substrate-bench-europe"
  }
}

locals {
  # The backend's bucket, which the backend block cannot take from here.
  state_bucket = "substrate-bench-europe-tfstate"
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
  region                = "europe-west1"
  default_resource_name = "substrate-bench-europe"
  create_jobs           = var.create_jobs

  state_bucket        = local.state_bucket
  state_bucket_prefix = "terraform"
  reports_bucket      = local.state_bucket
  reports_prefix      = "data"
  source_branch       = "main"

  # The cases scenarios.yaml's bare-metal suite lists.
  args = ["--suite=bare-metal"]

  # Daily, between substrate-bench-metal's and substrate-bench-east4's runs.
  schedule = "0 7 * * *"
}

output "cloud_run" {
  value = module.cloud_run
}
