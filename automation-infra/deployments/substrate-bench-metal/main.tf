terraform {
  required_version = ">= 1.5"

  required_providers {
    github = {
      source = "integrations/github"
    }
  }

  backend "gcs" {
    bucket = "substrate-bench-metal-tfstate"
    prefix = "automation/cloud-run/substrate-bench-metal"
  }
}

locals {
  # The backend's bucket, which the backend block cannot take from here.
  state_bucket = "substrate-bench-metal-tfstate"
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

# The bare-metal cases, apart from substrate-bench-auto, so that a wait for a
# machine holds up none of its runs, and in a region with C3 quota of its own.
# Its name must not start with substrate-bench-auto, or that deployment's
# alert would take its failures too.
module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = "akvelon-gke-aieco"
  region                = "us-east1"
  default_resource_name = "substrate-bench-metal"
  create_jobs           = var.create_jobs

  state_bucket        = local.state_bucket
  state_bucket_prefix = "terraform"
  reports_bucket      = local.state_bucket
  reports_prefix      = "data"
  source_branch       = "main"

  # The cases scenarios.yaml's bare-metal suite lists.
  args = ["--suite=bare-metal"]

  # Daily, between substrate-bench-auto's 21:00 and 03:00 runs.
  schedule = "0 1 * * *"
}

output "cloud_run" {
  value = module.cloud_run
}
