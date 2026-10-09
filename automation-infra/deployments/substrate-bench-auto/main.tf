terraform {
  required_version = ">= 1.5"

  backend "gcs" {
    bucket = "substrate-bench-auto-tfstate"
    prefix = "automation/cloud-run/substrate-bench-auto"
  }
}

locals {
  # The backend's bucket, which the backend block cannot take from here. Also
  # read by .github/workflows/upload-source.yml.
  state_bucket = "substrate-bench-auto-tfstate"
}

module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = "akvelon-gke-aieco"
  region                = "us-central1"
  default_resource_name = "substrate-bench-auto"

  state_bucket          = local.state_bucket
  state_bucket_prefix   = "terraform"
  github_actions_bucket = local.state_bucket
  source_branch         = "add-scheduler"

  # The dummy case, which creates no cluster, to check the schedule.
  schedule      = "*/2 * * * *"
  schedule_args = ["--scenario=dummy"]
}

output "cloud_run" {
  value = module.cloud_run
}
