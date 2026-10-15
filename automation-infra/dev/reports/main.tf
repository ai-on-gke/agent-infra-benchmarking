terraform {
  required_version = ">= 1.5"

  # Local state, in this folder: a dev reports deployment is one person's.
}

module "reports" {
  source = "../../terraform/modules/reports"

  project_id         = var.project_id
  region             = var.region
  name               = var.name
  writer_project_ids = var.writer_project_ids
}

# What the scenario-runner's --report-deployment=automation-infra/dev/reports
# sends a run's reports to.
output "reports" {
  value = module.reports
}
