terraform {
  required_version = ">= 1.5"

  backend "gcs" {
    bucket = "agent-substrate-auto-benchmarking-state"
    prefix = "automation/cloud-run/agent-substrate-bench-nightly"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.33.0, < 8.0.0"
    }
  }
}

locals {
  project_id = "akvelon-gke-aieco"
  region     = "us-central1"
}

provider "google" {
  project = local.project_id
  region  = local.region
}

module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = local.project_id
  region                = local.region
  default_resource_name = "agent-substrate-bench-nightly"

  env = {}

  cpu          = "2"
  memory       = "4Gi"
  task_timeout = "86400s"

  # 3am UTC every day.
  schedule  = "0 3 * * *"
  time_zone = "Etc/UTC"
}

output "cloud_run" {
  value = module.cloud_run
}
