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

  # The state bucket, from backend.hcl.
  github_actions_bucket = regex("bucket *= *\"([^\"]+)\"", file("${path.module}/backend.hcl"))[0]
}

output "cloud_run" {
  value = module.cloud_run
}
