terraform {
  required_version = ">= 1.5"

  backend "gcs" {
    bucket = "agent-substrate-bench-nightly-tfstate"
    prefix = "automation/cloud-run/agent-substrate-bench-nightly"
  }
}

module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = "akvelon-gke-aieco"
  region                = "us-central1"
  default_resource_name = "agent-substrate-bench-nightly"
}

output "cloud_run" {
  value = module.cloud_run
}
