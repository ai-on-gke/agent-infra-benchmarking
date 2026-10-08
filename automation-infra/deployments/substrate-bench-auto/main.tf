terraform {
  required_version = ">= 1.5"

  # Set by backend.hcl: terraform init -backend-config=backend.hcl
  backend "gcs" {}
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
