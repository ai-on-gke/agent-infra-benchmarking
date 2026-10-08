terraform {
  required_version = ">= 1.5"

  backend "gcs" {
    bucket = "substrate-bench-auto-tfstate"
    prefix = "automation/cloud-run/substrate-bench-auto"
  }
}

module "cloud_run" {
  source = "../../terraform/modules/cloud-run"

  project_id            = "akvelon-gke-aieco"
  region                = "us-central1"
  default_resource_name = "substrate-bench-auto"
}

output "cloud_run" {
  value = module.cloud_run
}
